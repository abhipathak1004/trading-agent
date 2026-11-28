import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bq.infra.database import migrate
from bq.infra.job_worker import JobWorker, JobWorkerLoop
from bq.infra.jobs import JobRun, JobRunStatus, JobRunStore

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def _store(tmp_path: Path) -> tuple[JobRunStore, MutableClock]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = MutableClock()
    return JobRunStore(path, clock), clock


def _enqueue(store: JobRunStore) -> JobRun:
    return store.enqueue(
        "memory.resolve",
        "memory.resolve:2026-11-26T12:00",
        scheduled_at=NOW - timedelta(minutes=5),
        max_attempts=2,
    )


def test_worker_dispatches_and_succeeds(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path)
    run = _enqueue(store)
    calls: list[str] = []
    worker = JobWorker(
        store, {"memory.resolve": lambda item: calls.append(item.run_id)}
    )
    completed = worker.run_once()
    assert completed is not None
    assert completed.status is JobRunStatus.SUCCEEDED
    assert calls == [run.run_id]


def test_worker_marks_missing_and_failed_handlers(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path)
    missing = store.enqueue(
        "memory.resolve",
        "memory.resolve:missing",
        scheduled_at=NOW - timedelta(minutes=6),
        max_attempts=1,
    )
    worker = JobWorker(store, {"other.job": lambda _item: None})
    result = worker.run_once()
    assert result is not None
    assert result.run_id == missing.run_id
    assert result.status is JobRunStatus.FAILED
    assert result.failure_code == "handler_missing"

    failed = store.enqueue(
        "memory.resolve",
        "memory.resolve:second",
        scheduled_at=NOW - timedelta(minutes=4),
        max_attempts=1,
    )

    def boom(_item: JobRun) -> None:
        raise RuntimeError("provider down")

    result = JobWorker(store, {"memory.resolve": boom}).run_once()
    assert result is not None
    assert result.run_id == failed.run_id
    assert result.status is JobRunStatus.FAILED
    assert result.failure_code == "handler_error"


def test_duplicate_occurrence_runs_once(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path)
    first = _enqueue(store)
    second = _enqueue(store)
    assert first.run_id == second.run_id
    calls: list[str] = []
    worker = JobWorker(
        store, {"memory.resolve": lambda item: calls.append(item.run_id)}
    )
    worker.run_until_idle()
    assert calls == [first.run_id]


def test_lease_expiry_is_reclaimed(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    run = _enqueue(store)
    claimed = store.claim_next("dead-worker", lease_for=timedelta(seconds=5))
    assert claimed is not None
    assert claimed.status is JobRunStatus.RUNNING
    clock.at = NOW + timedelta(seconds=6)
    calls: list[str] = []
    worker = JobWorker(
        store, {"memory.resolve": lambda item: calls.append(item.run_id)}
    )
    result = worker.run_once()
    assert result is not None
    assert result.run_id == run.run_id
    assert result.status is JobRunStatus.SUCCEEDED
    assert result.attempts == 2
    assert calls == [run.run_id]


def test_worker_loop_starts_and_stops(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path)
    worker = JobWorker(store, {"memory.resolve": lambda _item: None})
    loop = JobWorkerLoop(worker, interval=timedelta(seconds=30))

    async def check() -> None:
        loop.start()
        assert loop.running is True
        await loop.stop()
        assert loop.running is False

    asyncio.run(check())
