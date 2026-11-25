from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.news import DEFAULT_MEMORY_TTL, PULSE_FEED_URL
from bq.settings import (
    AppConfig,
    Provider,
    SettingsError,
    SettingsStore,
    mask_api_key,
    validate_api_key,
    validate_memory_ttl_days,
    validate_model_name,
    validate_news_feed_url,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _store(tmp_path: Path) -> SettingsStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return SettingsStore(path, FrozenClock())


def test_defaults_are_empty_and_deterministic(tmp_path: Path) -> None:
    config = _store(tmp_path).load()
    assert isinstance(config, AppConfig)
    assert config.provider is Provider.GEMINI
    assert config.model_name == ""
    assert config.news_feed_url == PULSE_FEED_URL
    assert config.news_memory_ttl_days == DEFAULT_MEMORY_TTL.days
    assert config.api_keys == ()
    assert config.model_route_configured is False
    assert config.api_keys_configured is False
    assert config.updated_at is None


def test_save_config_round_trips_and_stamps_updated_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = store.save_config(
        provider=Provider.DEEPSEEK,
        model_name="deepseek-chat",
        news_feed_url="https://example.com/feed.xml",
        news_memory_ttl_days=30,
    )
    assert config.provider is Provider.DEEPSEEK
    assert config.model_name == "deepseek-chat"
    assert config.news_feed_url == "https://example.com/feed.xml"
    assert config.news_memory_ttl_days == 30
    assert config.updated_at == NOW
    # Reloading is stable.
    assert store.load() == config


def test_model_route_requires_matching_provider_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_config(
        provider=Provider.GEMINI,
        model_name="gemini-2.5-pro",
        news_feed_url=PULSE_FEED_URL,
        news_memory_ttl_days=365,
    )
    # Model name alone is not a route.
    assert store.load().model_route_configured is False
    # A key for the wrong provider does not complete the route.
    store.set_api_key(Provider.DEEPSEEK, "sk-deepseek-abcdefgh")
    assert store.load().model_route_configured is False
    # The matching provider key completes it.
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-key-1234")
    config = store.load()
    assert config.model_route_configured is True
    assert config.api_keys_configured is True


def test_api_keys_are_masked_and_never_returned_in_plaintext(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = "AIza-secret-value-abcdefgh"
    store.set_api_key(Provider.GEMINI, key)
    config = store.load()
    assert config.api_keys[0].masked == mask_api_key(key)
    assert key not in config.api_keys[0].masked
    assert config.api_keys[0].masked.endswith("efgh")
    # The internal accessor still returns the real key.
    assert store.api_key(Provider.GEMINI) == key
    store.clear_api_key(Provider.GEMINI)
    assert store.api_key(Provider.GEMINI) is None


def test_mask_and_validation_bounds() -> None:
    assert mask_api_key("short") == "*****"
    assert mask_api_key("AIza-secret-value-abcdefgh") == "********efgh"
    assert validate_model_name("  gemini-2.5-pro  ") == "gemini-2.5-pro"
    with pytest.raises(SettingsError, match="control"):
        validate_model_name("bad\x00name")
    with pytest.raises(SettingsError, match="at most"):
        validate_model_name("x" * 201)
    with pytest.raises(SettingsError, match="HTTPS"):
        validate_news_feed_url("http://example.com/feed.xml")
    with pytest.raises(SettingsError, match="credentials"):
        validate_news_feed_url("https://user:pass@example.com/feed.xml")
    assert validate_news_feed_url("https://example.com/feed.xml") == (
        "https://example.com/feed.xml"
    )
    with pytest.raises(SettingsError, match="between"):
        validate_memory_ttl_days(0)
    with pytest.raises(SettingsError, match="whole number"):
        validate_memory_ttl_days(True)
    with pytest.raises(SettingsError, match="between"):
        validate_api_key("short")
    assert validate_api_key("  AIza-key-12345678  ") == "AIza-key-12345678"


def test_save_config_rejects_invalid_values(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SettingsError):
        store.save_config(
            provider=Provider.GEMINI,
            model_name="ok",
            news_feed_url="http://insecure.example.com/feed.xml",
            news_memory_ttl_days=30,
        )
    with pytest.raises(SettingsError):
        store.save_config(
            provider=Provider.GEMINI,
            model_name="ok",
            news_feed_url=PULSE_FEED_URL,
            news_memory_ttl_days=0,
        )


def test_settings_changes_are_audited_without_secret_material(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    store = _store(tmp_path)
    store.set_api_key(Provider.GEMINI, "AIza-secret-value-abcdefgh")
    with closing(sqlite3.connect(tmp_path / "bq.db")) as connection:
        rows = connection.execute(
            "SELECT action, object_id, payload_hash FROM audit_events ORDER BY sequence"
        ).fetchall()
    actions = {row[0] for row in rows}
    assert "settings.api_key.set" in actions
    # Audit rows carry only a hash of the payload, never the key itself.
    assert all(len(row[2]) == 64 for row in rows)
    assert all("AIza-secret" not in str(row[1]) for row in rows)


def test_settings_show_cli_reports_masked_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from bq.cli import main

    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    assert main(["init"]) == 0
    path = tmp_path / "bq.db"
    store = SettingsStore(path, FrozenClock())
    store.save_config(
        provider=Provider.GEMINI,
        model_name="gemini-2.5-pro",
        news_feed_url="https://example.com/feed.xml",
        news_memory_ttl_days=90,
    )
    store.set_api_key(Provider.GEMINI, "AIza-secret-value-abcdefgh")
    capsys.readouterr()

    assert main(["settings", "show"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["provider"] == "GEMINI"
    assert output["model_name"] == "gemini-2.5-pro"
    assert output["news_memory_ttl_days"] == 90
    assert output["model_route_configured"] is True
    assert output["api_keys"] == [
        {
            "provider": "GEMINI",
            "configured": True,
            "masked": "********efgh",
        }
    ]
    assert "AIza-secret-value-abcdefgh" not in json.dumps(output)


def test_news_agent_honours_configured_memory_ttl(tmp_path: Path) -> None:
    from bq.news import PULSE_SOURCE_ID
    from bq.news_agent import NewsResearchAgent

    path = tmp_path / "bq.db"
    migrate(path, create=True)
    feed = b"""<rss><channel><item>
        <title>SEBI fines broker for insider trading</title>
        <description>Regulator issues penalty.</description>
        <link>https://example.com/sebi</link>
    </item></channel></rss>"""
    agent = NewsResearchAgent(path, clock=FrozenClock(), source_id=PULSE_SOURCE_ID)
    report = agent.run(payload=feed, memory_ttl_days=7)
    assert report.long_term_memories == 1
    memory = agent.digest()[0]
    assert memory.expires_at is not None
    assert (memory.expires_at - memory.known_at).days == 7
