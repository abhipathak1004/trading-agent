import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.memory import (
    MissingPlaybook,
    PlaybookStore,
    ProceduralError,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _store(tmp_path: Path) -> PlaybookStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return PlaybookStore(path, FrozenClock())


def test_defaults_are_code_owned_and_listed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    playbooks = store.list()
    assert {item.playbook_id for item in playbooks} == {
        "momentum_continuation",
        "defensive",
        "event_aware",
    }
    defensive = store.get("defensive")
    assert defensive.version == 0
    assert defensive.is_default is True
    assert defensive.updated_at is None
    assert "Capital preservation" in defensive.content


def test_save_appends_versions_and_reset_returns_to_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.save("defensive", content="CUSTOM GUIDANCE")
    assert saved.version == 1
    assert saved.content == "CUSTOM GUIDANCE"
    assert saved.is_default is False
    assert saved.updated_at == NOW
    assert store.get("defensive").content == "CUSTOM GUIDANCE"

    reset = store.reset("defensive")
    assert reset.version == 2
    assert reset.is_default is True


def test_invalid_playbooks_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(MissingPlaybook, match="Unknown"):
        store.save("not-a-playbook", content="x")
    with pytest.raises(ProceduralError, match="empty"):
        store.save("defensive", content="")
    with pytest.raises(ProceduralError, match="control"):
        store.save("defensive", content="bad\x00content")
    with pytest.raises(ProceduralError, match="at most"):
        store.save("defensive", content="x" * 20_000)


def test_playbooks_are_immutable_and_audited_without_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save("defensive", content="SECRET PLAYBOOK TEXT")
    with closing(sqlite3.connect(tmp_path / "bq.db")) as connection:
        rows = connection.execute(
            "SELECT action, object_id, payload_hash FROM audit_events ORDER BY sequence"
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE procedural_playbooks SET content = 'x' WHERE playbook_id = 'defensive'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM procedural_playbooks WHERE playbook_id = 'defensive'"
            )
    assert any(row[0] == "memory.playbook.saved" for row in rows)
    assert all(len(row[2]) == 64 for row in rows)
    assert all("SECRET" not in str(row) for row in rows)
