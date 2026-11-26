from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq.infra.database import check_database, migrate
from bq.infra.jobs import JobRunStore
from bq.runtime import (
    RuntimeError,
    backup_database,
    benchmark,
    inspect_runtime,
    metrics_text,
    restore_database,
)

NOW = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def test_runtime_health_metrics_and_stale_jobs(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    status = inspect_runtime(path, now=NOW)
    assert status["status"] == "ok"
    assert status["migration_count"] >= 36
    assert status["jobs"]["pending"] == 0
    assert "bq_runtime_degraded 0" in metrics_text(status)

    jobs = JobRunStore(path, FrozenClock())
    jobs.enqueue(
        "memory.resolve",
        "runtime-stale",
        scheduled_at=NOW,
        max_attempts=2,
    )
    claimed = jobs.claim_next("worker-1", lease_for=timedelta(minutes=1))
    assert claimed is not None
    stale = inspect_runtime(path, now=NOW + timedelta(minutes=20))
    assert stale["jobs"]["stale"] == 1
    assert stale["status"] == "degraded"


def test_backup_restore_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    backup = tmp_path / "backup.db"
    manifest = backup_database(path, backup)
    assert backup.is_file()
    assert len(str(manifest["sha256"])) == 64
    assert (tmp_path / "backup.db.json").is_file()
    check_database(backup)

    restored = tmp_path / "restored.db"
    result = restore_database(backup, restored, confirm=True)
    assert result["target"] == str(restored)
    check_database(restored)
    with pytest.raises(RuntimeError, match="confirmation"):
        restore_database(backup, restored)
    with pytest.raises(RuntimeError, match="already exists"):
        backup_database(path, backup)


def test_benchmark_is_bounded() -> None:
    result = benchmark()
    assert result["status"] == "ok"
    assert float(result["digest_10000_ms"]) >= 0
