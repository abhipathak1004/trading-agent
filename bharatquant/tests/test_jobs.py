import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

import pytest

from bq.infra.database import check_database, migrate
from bq.infra.jobs import (
    JobRun,
    JobRunConflict,
    JobRunError,
    JobRunStatus,
    JobRunStore,
)


class FrozenClock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


def test_job_occurrence_is_idempotent_and_requires_matching_parameters(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = JobRunStore(path, FrozenClock())
    scheduled_at = datetime(2026, 11, 21, 14, 0, tzinfo=UTC)

    original = store.enqueue(
        "daily-report",
        "2026-11-21T14:00Z",
        scheduled_at=scheduled_at,
        max_attempts=3,
    )
    duplicate = store.enqueue(
        "daily-report",
        "2026-11-21T14:00Z",
        scheduled_at=scheduled_at,
        max_attempts=3,
    )
    assert duplicate == original
    with pytest.raises(JobRunConflict, match="different parameters"):
        store.enqueue(
            "daily-report",
            "2026-11-21T14:00Z",
            scheduled_at=scheduled_at,
            max_attempts=4,
        )
    assert [event.event_type for event in store.events_for(original.run_id)] == [
        "ENQUEUED"
    ]


@pytest.mark.parametrize(("run_prefix", "lease_prefix"), [("_", "-"), ("-", "_")])
def test_generated_urlsafe_identifiers_can_start_with_dash_or_underscore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_prefix: str, lease_prefix: str
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = JobRunStore(path, FrozenClock())
    monkeypatch.setattr(
        "bq.infra.jobs.secrets.token_urlsafe",
        lambda size: f"{run_prefix}run" if size == 18 else f"{lease_prefix}lease",
    )
    run = store.enqueue(
        "job", "occurrence", scheduled_at=FrozenClock().now(), max_attempts=1
    )
    claimed = store.claim_next("worker", lease_for=timedelta(minutes=1))
    assert claimed is not None and claimed.lease_token is not None
    assert store.get(run.run_id) == claimed
    assert (
        store.succeed(run.run_id, "worker", lease_token=claimed.lease_token).status
        is JobRunStatus.SUCCEEDED
    )
    assert len(store.events_for(run.run_id)) == 3


def test_job_runs_wait_until_due_and_reject_stale_workers(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = JobRunStore(path, clock)
    scheduled_at = clock.now() + timedelta(minutes=5)
    run = store.enqueue(
        "refresh", "occurrence-1", scheduled_at=scheduled_at, max_attempts=3
    )

    assert store.claim_next("worker-a", lease_for=timedelta(minutes=1)) is None
    clock.instant = scheduled_at
    claimed = store.claim_next("worker-a", lease_for=timedelta(minutes=1))
    assert claimed is not None
    assert claimed.run_id == run.run_id
    assert claimed.status is JobRunStatus.RUNNING
    assert claimed.attempts == 1
    first_lease_token = claimed.lease_token
    assert first_lease_token is not None

    clock.instant += timedelta(minutes=1)
    reclaimed = store.claim_next("worker-a", lease_for=timedelta(minutes=1))
    assert reclaimed is not None
    assert reclaimed.attempts == 2
    assert reclaimed.lease_owner == "worker-a"
    assert reclaimed.lease_token != first_lease_token
    current_lease_token = reclaimed.lease_token
    assert current_lease_token is not None
    with pytest.raises(JobRunConflict, match="active job lease"):
        store.succeed(run.run_id, "worker-a", lease_token=first_lease_token)
    assert (
        store.succeed(run.run_id, "worker-a", lease_token=current_lease_token).status
        is JobRunStatus.SUCCEEDED
    )
    assert [event.event_type for event in store.events_for(run.run_id)] == [
        "ENQUEUED",
        "CLAIMED",
        "LEASE_RECLAIMED",
        "SUCCEEDED",
    ]


def test_concurrent_workers_cannot_claim_the_same_occurrence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = JobRunStore(path, clock)
    store.enqueue(
        "snapshot",
        "occurrence-1",
        scheduled_at=clock.now(),
        max_attempts=3,
    )
    barrier = Barrier(2)

    def claim(worker_id: str) -> JobRun | None:
        barrier.wait()
        return store.claim_next(worker_id, lease_for=timedelta(minutes=1))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-a", "worker-b")))

    assert sum(result is not None for result in results) == 1


def test_failures_retry_within_limit_then_become_terminal(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = JobRunStore(path, clock)
    run = store.enqueue(
        "daily-report",
        "occurrence-1",
        scheduled_at=clock.now(),
        max_attempts=2,
    )

    first = store.claim_next("worker-a", lease_for=timedelta(minutes=1))
    assert first is not None
    assert first.lease_token is not None
    retry = store.fail(
        run.run_id,
        "worker-a",
        "provider_timeout",
        lease_token=first.lease_token,
    )
    assert retry.status is JobRunStatus.PENDING
    assert retry.failure_code == "provider_timeout"

    second = store.claim_next("worker-b", lease_for=timedelta(minutes=1))
    assert second is not None
    assert second.lease_token is not None
    failed = store.fail(
        run.run_id,
        "worker-b",
        "provider_timeout",
        lease_token=second.lease_token,
    )
    assert failed.status is JobRunStatus.FAILED
    assert store.claim_next("worker-c", lease_for=timedelta(minutes=1)) is None
    assert [event.event_type for event in store.events_for(run.run_id)] == [
        "ENQUEUED",
        "CLAIMED",
        "RETRY_SCHEDULED",
        "CLAIMED",
        "FAILED",
    ]


def test_expired_last_lease_fails_and_job_events_are_append_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = JobRunStore(path, clock)
    run = store.enqueue(
        "backup", "occurrence-1", scheduled_at=clock.now(), max_attempts=1
    )
    assert store.claim_next("worker-a", lease_for=timedelta(seconds=30)) is not None
    clock.instant += timedelta(seconds=30)
    assert store.claim_next("worker-b", lease_for=timedelta(seconds=30)) is None
    assert store.get(run.run_id).status is JobRunStatus.FAILED
    assert store.events_for(run.run_id)[-1].event_type == "LEASE_EXPIRED"

    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE job_run_events SET failure_code = 'changed' WHERE run_id = ?",
                (run.run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute("DELETE FROM job_runs WHERE run_id = ?", (run.run_id,))
    check_database(path)


def test_job_run_rejects_invalid_retry_and_lease_inputs(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = JobRunStore(path, FrozenClock())
    with pytest.raises(ValueError, match="positive"):
        store.enqueue(
            "report",
            "occurrence-1",
            scheduled_at=FrozenClock().now(),
            max_attempts=0,
        )
    run = store.enqueue(
        "report",
        "occurrence-1",
        scheduled_at=FrozenClock().now(),
        max_attempts=3,
    )
    with pytest.raises(ValueError, match="positive"):
        store.claim_next("worker", lease_for=timedelta(0))
    with pytest.raises(ValueError, match="failure code"):
        store.fail(
            run.run_id,
            "worker",
            "Provider timeout",
            lease_token="invalid-lease-token",
        )
    with pytest.raises(JobRunError, match="does not exist"):
        store.events_for("missing-run")


def test_expired_lease_cannot_finish_after_waiting_for_a_database_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = JobRunStore(path, clock)
    run = store.enqueue(
        "report", "occurrence", scheduled_at=clock.now(), max_attempts=2
    )
    claimed = store.claim_next("worker", lease_for=timedelta(seconds=30))
    assert claimed is not None
    assert claimed.lease_token is not None
    token = claimed.lease_token
    entered = Event()
    original_connect = store._connect

    def traced_connect() -> sqlite3.Connection:
        connection = original_connect()
        connection.set_trace_callback(
            lambda sql: entered.set() if sql == "BEGIN IMMEDIATE" else None
        )
        return connection

    monkeypatch.setattr(store, "_connect", traced_connect)
    with closing(sqlite3.connect(path)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                store.succeed, run.run_id, "worker", lease_token=token
            )
            try:
                assert entered.wait(timeout=5)
                clock.instant += timedelta(seconds=30)
            finally:
                blocker.commit()
            with pytest.raises(JobRunConflict, match="active job lease"):
                future.result(timeout=5)


def test_claim_uses_current_time_after_waiting_for_a_database_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = JobRunStore(path, clock)
    store.enqueue(
        "report",
        "occurrence",
        scheduled_at=clock.now() + timedelta(seconds=30),
        max_attempts=2,
    )
    entered = Event()
    original_connect = store._connect

    def traced_connect() -> sqlite3.Connection:
        connection = original_connect()
        connection.set_trace_callback(
            lambda sql: entered.set() if sql == "BEGIN IMMEDIATE" else None
        )
        return connection

    monkeypatch.setattr(store, "_connect", traced_connect)
    with closing(sqlite3.connect(path)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                store.claim_next, "worker", lease_for=timedelta(seconds=30)
            )
            try:
                assert entered.wait(timeout=5)
                clock.instant += timedelta(seconds=30)
            finally:
                blocker.commit()
            claimed = future.result(timeout=5)
    assert claimed is not None
    assert claimed.lease_until == clock.now() + timedelta(seconds=30)
