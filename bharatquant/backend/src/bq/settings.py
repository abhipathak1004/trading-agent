"""Owner-managed application configuration.

Non-secret values live in ``app_settings``; provider API keys live in
``provider_api_keys``. This module is the only writer of either table.

Two deliberate limits:

* Every value is bounded before it is stored, so a typo cannot inflate a row and
  every read stays cheap.
* API keys are never returned in plaintext by ``load``. It reports only whether
  a key exists and a masked hint. The key itself is stored in plaintext in the
  local database, which is acceptable for a single-owner, loopback-only
  application but is not vault-grade protection.
"""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from bq.core import Clock, SystemClock
from bq.infra.audit import append_event
from bq.news import DEFAULT_MEMORY_TTL, PULSE_FEED_URL

DEFAULT_NEWS_MEMORY_TTL_DAYS = DEFAULT_MEMORY_TTL.days
MAX_MODEL_NAME_CHARS = 200
MIN_MEMORY_TTL_DAYS = 1
MAX_MEMORY_TTL_DAYS = 3650
MAX_API_KEY_CHARS = 2048
MIN_API_KEY_CHARS = 8
MAX_SETTING_CHARS = 4096
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

KEY_PROVIDER = "provider"
KEY_MODEL_NAME = "model_name"
KEY_NEWS_FEED_URL = "news_feed_url"
KEY_NEWS_MEMORY_TTL_DAYS = "news_memory_ttl_days"
KEY_KITE_ENABLED = "kite_enabled"


class SettingsError(ValueError):
    pass


class Provider(StrEnum):
    GEMINI = "GEMINI"
    DEEPSEEK = "DEEPSEEK"
    OPENAI = "OPENAI"
    CUSTOM = "CUSTOM"


DEFAULT_PROVIDER = Provider.GEMINI


@dataclass(frozen=True, slots=True)
class ApiKeyStatus:
    provider: Provider
    configured: bool
    masked: str | None


@dataclass(frozen=True, slots=True)
class AppConfig:
    provider: Provider
    model_name: str
    news_feed_url: str
    news_memory_ttl_days: int
    api_keys: tuple[ApiKeyStatus, ...]
    updated_at: datetime | None
    kite_enabled: bool = False

    @property
    def model_route_configured(self) -> bool:
        """True only when a model name and the matching provider key exist."""
        if not self.model_name:
            return False
        return any(
            key.provider is self.provider and key.configured for key in self.api_keys
        )

    @property
    def api_keys_configured(self) -> bool:
        return any(key.configured for key in self.api_keys)


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise SettingsError("Configuration timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _require_provider(provider: Provider | str) -> Provider:
    try:
        return provider if isinstance(provider, Provider) else Provider(provider)
    except ValueError as exc:
        raise SettingsError("Unknown model provider.") from exc


def validate_model_name(value: str) -> str:
    if not isinstance(value, str):
        raise SettingsError("Model name must be text.")
    name = value.strip()
    if len(name) > MAX_MODEL_NAME_CHARS:
        raise SettingsError(
            f"Model name must be at most {MAX_MODEL_NAME_CHARS} characters."
        )
    if _CONTROL.search(name):
        raise SettingsError("Model name must not contain control characters.")
    return name


def validate_news_feed_url(value: str) -> str:
    if not isinstance(value, str):
        raise SettingsError("Feed URL must be text.")
    url = value.strip()
    if len(url) > MAX_SETTING_CHARS:
        raise SettingsError("Feed URL is too long.")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise SettingsError("Feed URL must be HTTPS without embedded credentials.")
    return url


def validate_memory_ttl_days(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError("Memory retention must be a whole number of days.")
    if not MIN_MEMORY_TTL_DAYS <= value <= MAX_MEMORY_TTL_DAYS:
        raise SettingsError(
            f"Memory retention must be between {MIN_MEMORY_TTL_DAYS} and "
            f"{MAX_MEMORY_TTL_DAYS} days."
        )
    return value


def validate_api_key(value: str) -> str:
    if not isinstance(value, str):
        raise SettingsError("API key must be text.")
    key = value.strip()
    if not MIN_API_KEY_CHARS <= len(key) <= MAX_API_KEY_CHARS:
        raise SettingsError(
            f"API key must be between {MIN_API_KEY_CHARS} and "
            f"{MAX_API_KEY_CHARS} characters."
        )
    if not key.isprintable() or any(char.isspace() for char in key):
        raise SettingsError("API key must be a single printable token.")
    return key


def mask_api_key(value: str) -> str:
    """Show enough to recognise the key, never enough to use it."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{'*' * 8}{value[-4:]}"


class SettingsStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )

    def load(self) -> AppConfig:
        with closing(self._connect()) as connection:
            rows = dict(
                connection.execute(
                    "SELECT setting_key, setting_value FROM app_settings"
                ).fetchall()
            )
            key_rows = connection.execute(
                "SELECT provider, api_key, updated_at FROM provider_api_keys"
            ).fetchall()
            latest = connection.execute(
                "SELECT MAX(updated_at) FROM ("
                "SELECT updated_at FROM app_settings "
                "UNION ALL SELECT updated_at FROM provider_api_keys)"
            ).fetchone()
        provider = DEFAULT_PROVIDER
        if KEY_PROVIDER in rows:
            try:
                provider = Provider(str(rows[KEY_PROVIDER]))
            except ValueError:
                provider = DEFAULT_PROVIDER
        ttl = DEFAULT_NEWS_MEMORY_TTL_DAYS
        if KEY_NEWS_MEMORY_TTL_DAYS in rows:
            try:
                ttl = int(str(rows[KEY_NEWS_MEMORY_TTL_DAYS]))
            except ValueError:
                ttl = DEFAULT_NEWS_MEMORY_TTL_DAYS
        updated = None
        if latest is not None and latest[0] is not None:
            updated = _parse_timestamp(str(latest[0]))
        # Absent means off: a fresh install must not attempt broker calls.
        kite_enabled = str(rows.get(KEY_KITE_ENABLED, "false")).lower() == "true"
        return AppConfig(
            provider=provider,
            model_name=str(rows.get(KEY_MODEL_NAME, "")),
            news_feed_url=str(rows.get(KEY_NEWS_FEED_URL, PULSE_FEED_URL)),
            news_memory_ttl_days=ttl,
            api_keys=tuple(
                ApiKeyStatus(
                    provider=Provider(str(row[0])),
                    configured=True,
                    masked=mask_api_key(str(row[1])),
                )
                for row in sorted(key_rows, key=lambda item: str(item[0]))
            ),
            updated_at=updated,
            kite_enabled=kite_enabled,
        )

    def api_key(self, provider: Provider | str) -> str | None:
        """Return the stored key for internal clients. Never expose via the API."""
        target = _require_provider(provider)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT api_key FROM provider_api_keys WHERE provider = ?",
                (target.value,),
            ).fetchone()
        return None if row is None else str(row[0])

    def save_config(
        self,
        *,
        provider: Provider | str,
        model_name: str,
        news_feed_url: str,
        news_memory_ttl_days: int,
    ) -> AppConfig:
        target = _require_provider(provider)
        name = validate_model_name(model_name)
        feed_url = validate_news_feed_url(news_feed_url)
        ttl = validate_memory_ttl_days(news_memory_ttl_days)
        updated_at = _utc(self.clock.now())
        values = {
            KEY_PROVIDER: target.value,
            KEY_MODEL_NAME: name,
            KEY_NEWS_FEED_URL: feed_url,
            KEY_NEWS_MEMORY_TTL_DAYS: str(ttl),
        }
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                for key, value in values.items():
                    connection.execute(
                        "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(setting_key) DO UPDATE SET "
                        "setting_value = excluded.setting_value, "
                        "updated_at = excluded.updated_at",
                        (key, value, _timestamp(updated_at)),
                    )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.config.updated",
                    object_type="app_settings",
                    object_id="config",
                    payload={
                        "provider": target.value,
                        "model_name": name,
                        "news_feed_url": feed_url,
                        "news_memory_ttl_days": str(ttl),
                    },
                )
        return self.load()

    def set_api_key(self, provider: Provider | str, api_key: str) -> None:
        target = _require_provider(provider)
        key = validate_api_key(api_key)
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO provider_api_keys (provider, api_key, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(provider) DO UPDATE SET "
                    "api_key = excluded.api_key, updated_at = excluded.updated_at",
                    (target.value, key, _timestamp(updated_at)),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.api_key.set",
                    object_type="provider_api_key",
                    object_id=target.value,
                    payload={"masked": mask_api_key(key)},
                )

    def clear_api_key(self, provider: Provider | str) -> None:
        target = _require_provider(provider)
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "DELETE FROM provider_api_keys WHERE provider = ?",
                    (target.value,),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.api_key.cleared",
                    object_type="provider_api_key",
                    object_id=target.value,
                    payload={},
                )
