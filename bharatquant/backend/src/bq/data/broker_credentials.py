"""Broker credentials for read-only Kite Connect access.

Separate from ``bq.settings`` and from ``provider_api_keys`` on purpose: Kite is
a broker, not a model provider. Keeping it apart means Kite can never be offered
as an LLM route, and clearing a model key cannot silently break market data.

The storage limitation is the same one ``bq.settings`` documents: values are
plaintext in the local SQLite database, which is acceptable for a single-owner
loopback application and is not vault-grade. What is never done is returning a
secret to the browser — ``status()`` reports only whether a credential exists
and a masked hint.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bq.core import (
    Clock,
    SystemClock,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    parse_timestamp as core_parse_timestamp,
)
from bq.core import (
    utc as core_utc,
)
from bq.infra.audit import append_event
from bq.settings import mask_api_key


def _utc(value: datetime) -> datetime:
    try:
        return core_utc(value)
    except ValueError as exc:
        raise BrokerCredentialError(
            "Credential timestamps must be timezone-aware."
        ) from exc


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return core_parse_timestamp(value)


BROKER_KITE = "KITE"
MIN_SECRET_CHARS = 8
MAX_SECRET_CHARS = 512


class BrokerCredentialError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class KiteStatus:
    """What the UI and ``bq doctor`` are allowed to see."""

    configured: bool
    api_key_masked: str | None
    updated_at: datetime | None


def _validate_secret(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise BrokerCredentialError(f"{label} must be text.")
    secret = value.strip()
    if not MIN_SECRET_CHARS <= len(secret) <= MAX_SECRET_CHARS:
        raise BrokerCredentialError(
            f"{label} must be between {MIN_SECRET_CHARS} and "
            f"{MAX_SECRET_CHARS} characters."
        )
    if not secret.isprintable() or any(char.isspace() for char in secret):
        raise BrokerCredentialError(f"{label} must be a single printable token.")
    return secret


class BrokerCredentialStore:
    """Owns the ``broker_credentials`` row for Kite."""

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=False)

    def status(self) -> KiteStatus:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT api_key, updated_at FROM broker_credentials WHERE broker = ?",
                (BROKER_KITE,),
            ).fetchone()
        if row is None:
            return KiteStatus(False, None, None)
        return KiteStatus(
            configured=True,
            api_key_masked=mask_api_key(str(row[0])),
            updated_at=_parse_timestamp(str(row[1])),
        )

    def credentials(self) -> tuple[str, str, str] | None:
        """Return ``(api_key, api_secret, access_token)`` for internal clients.

        Never expose the result through an HTTP response.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT api_key, api_secret, access_token FROM broker_credentials "
                "WHERE broker = ?",
                (BROKER_KITE,),
            ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1]), str(row[2])

    def save(self, *, api_key: str, api_secret: str, access_token: str) -> KiteStatus:
        key = _validate_secret(api_key, "Kite API key")
        secret = _validate_secret(api_secret, "Kite API secret")
        token = _validate_secret(access_token, "Kite access token")
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO broker_credentials "
                    "(broker, api_key, api_secret, access_token, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(broker) DO UPDATE SET "
                    "api_key = excluded.api_key, "
                    "api_secret = excluded.api_secret, "
                    "access_token = excluded.access_token, "
                    "updated_at = excluded.updated_at",
                    (BROKER_KITE, key, secret, token, _timestamp(updated_at)),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="broker.credentials.set",
                    object_type="broker_credential",
                    object_id=BROKER_KITE,
                    payload={"api_key_masked": mask_api_key(key)},
                )
        return self.status()

    def clear(self) -> None:
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "DELETE FROM broker_credentials WHERE broker = ?",
                    (BROKER_KITE,),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="broker.credentials.cleared",
                    object_type="broker_credential",
                    object_id=BROKER_KITE,
                    payload={},
                )


__all__ = [
    "BROKER_KITE",
    "BrokerCredentialError",
    "BrokerCredentialStore",
    "KiteStatus",
]
