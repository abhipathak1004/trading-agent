import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.logbook import LoggingConfig
from bq.settings import SettingsError, SettingsStore

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
STAMP = NOW.isoformat().replace("+00:00", "Z")


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _store(tmp_path: Path) -> SettingsStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return SettingsStore(path, FrozenClock())


def test_logging_defaults_are_additive(tmp_path: Path) -> None:
    config = _store(tmp_path).load()
    assert config.logging == LoggingConfig()
    assert config.logging.enabled is True
    assert config.logging.retention_days == 14
    assert config.logging.flush_minutes == 15


def test_logging_round_trips_and_is_audited(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.save_logging(
        LoggingConfig(enabled=False, retention_days=90, flush_minutes=30)
    )
    assert saved.logging == LoggingConfig(
        enabled=False, retention_days=90, flush_minutes=30
    )
    assert store.load() == saved

    with sqlite3.connect(tmp_path / "bq.db") as connection:
        actions = [
            row[0]
            for row in connection.execute(
                "SELECT action FROM audit_events ORDER BY sequence"
            )
        ]
    assert "settings.logging.updated" in actions


def test_logging_clear_restores_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_logging(LoggingConfig(retention_days=1, flush_minutes=5))
    cleared = store.clear_logging()
    assert cleared.logging == LoggingConfig()
    with sqlite3.connect(tmp_path / "bq.db") as connection:
        actions = [
            row[0]
            for row in connection.execute(
                "SELECT action FROM audit_events ORDER BY sequence"
            )
        ]
    assert "settings.logging.cleared" in actions


def test_logging_rejects_a_foreign_object(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SettingsError):
        store.save_logging("not a config")  # type: ignore[arg-type]


def test_corrupt_logging_row_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
            "VALUES (?, ?, ?)",
            ("logging", "{not json", STAMP),
        )
    assert SettingsStore(path).load().logging == LoggingConfig()
