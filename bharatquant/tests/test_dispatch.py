"""
Tests for infra/dispatch.py — wake-to-job dispatch.

Each test uses a fresh in-memory database (via tmp_path) with all migrations
applied.  Schedules are saved with an effective boundary so that
ScheduleStore.effective_at() can resolve them.

The job_id used throughout is ``"synthetic-report"``, which is in the
``AVAILABLE_JOBS`` set passed to the ScheduleStore.
"""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from bq.infra.database import migrate
from bq.infra.dispatch import (
    _occurrence_key,
    dispatch_due,
    dispatch_one,
)
from bq.infra.jobs import JobRunStatus, JobRunStore
from bq.infra.schedules import ScheduleStore, WakeDecisionRecord
from bq.schedules import DailySchedule, WakeDecision, WakePolicy

AVAILABLE_JOBS: frozenset[str] = frozenset({"synthetic-report"})

# Reference wall-clock instant used by the MovingClock below.
_BASE = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)
# 08:00 IST on 2026-11-21 == 02:30 UTC
_OCCURRENCE = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)


class MovingClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def _make_stores(
    path: Path,
    *,
    clock_instant: datetime | None = None,
) -> tuple[ScheduleStore, JobRunStore]:
    migrate(path, create=True)
    clock = MovingClock(
        clock_instant if clock_instant is not None else _OCCURRENCE - timedelta(hours=1)
    )
    ss = ScheduleStore(path, available_jobs=AVAILABLE_JOBS, clock=clock)
    js = JobRunStore(path, clock)
    return ss, js


def _daily_schedule(
    schedule_id: str = "report",
    job_id: str = "synthetic-report",
    max_attempts: int = 2,
) -> DailySchedule:
    return DailySchedule(
        schedule_id=schedule_id,
        job_id=job_id,
        enabled=True,
        timezone="Asia/Kolkata",
        local_time=time(8),
        window_start=time(7, 45),
        window_end=time(17, 30),
        wake_policy=WakePolicy.RUN_ON_WAKE_WITHIN,
        wake_within_seconds=1800,
        max_attempts=max_attempts,
    )


def _run_decision(
    schedule_id: str = "report",
    scheduled_at: datetime = _OCCURRENCE,
    decision: WakeDecision = WakeDecision.RUN,
    schedule_version: int = 1,
) -> WakeDecisionRecord:
    return WakeDecisionRecord(
        schedule_id=schedule_id,
        scheduled_at=scheduled_at,
        observed_at=_BASE,
        schedule_version=schedule_version,
        decision=decision,
    )


# ---------------------------------------------------------------------------
# _occurrence_key
# ---------------------------------------------------------------------------


def test_occurrence_key_is_stable() -> None:
    record = _run_decision()
    key = _occurrence_key(record)
    assert (
        key
        == f"report:{_OCCURRENCE.isoformat(timespec='microseconds').replace('+00:00', 'Z')}"
    )
    assert key == _occurrence_key(record)


def test_occurrence_key_includes_schedule_id() -> None:
    a = _occurrence_key(_run_decision(schedule_id="morning"))
    b = _occurrence_key(_run_decision(schedule_id="evening"))
    assert a != b
    assert "morning" in a
    assert "evening" in b


def test_occurrence_key_different_times_are_different() -> None:
    t1 = _occurrence_key(_run_decision(scheduled_at=_OCCURRENCE))
    t2 = _occurrence_key(_run_decision(scheduled_at=_OCCURRENCE + timedelta(minutes=1)))
    assert t1 != t2


# ---------------------------------------------------------------------------
# dispatch_one — skipped decisions
# ---------------------------------------------------------------------------


def test_dispatch_one_skips_non_run_decisions(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    ss.save(
        (_daily_schedule(),),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    for decision in (
        WakeDecision.SKIPPED_BY_POLICY,
        WakeDecision.WAKE_WINDOW_EXPIRED,
        WakeDecision.OUTSIDE_OPERATING_WINDOW,
    ):
        record = _run_decision(decision=decision)
        result = dispatch_one(record, schedule_store=ss, job_store=js)
        assert result.skipped is True
        assert result.run is None
        assert result.error is None


# ---------------------------------------------------------------------------
# dispatch_one — successful enqueue
# ---------------------------------------------------------------------------


def test_dispatch_one_enqueues_run_decision(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    ss.save(
        (_daily_schedule(),),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    record = _run_decision()
    result = dispatch_one(record, schedule_store=ss, job_store=js)

    assert result.skipped is False
    assert result.error is None
    assert result.run is not None
    assert result.run.job_id == "synthetic-report"
    assert result.run.status is JobRunStatus.PENDING
    assert result.run.max_attempts == 2
    assert result.run.occurrence_key == _occurrence_key(record)
    assert result.run.scheduled_at == _OCCURRENCE


def test_dispatch_one_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    ss.save(
        (_daily_schedule(),),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    record = _run_decision()
    first = dispatch_one(record, schedule_store=ss, job_store=js)
    second = dispatch_one(record, schedule_store=ss, job_store=js)

    assert first.run is not None
    assert second.run is not None
    # Same run_id means idempotent enqueue returned the same row.
    assert first.run.run_id == second.run.run_id


def test_dispatch_one_max_attempts_comes_from_schedule(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    ss.save(
        (_daily_schedule(max_attempts=5),),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    result = dispatch_one(_run_decision(), schedule_store=ss, job_store=js)
    assert result.run is not None
    assert result.run.max_attempts == 5


# ---------------------------------------------------------------------------
# dispatch_one — error paths
# ---------------------------------------------------------------------------


def test_dispatch_one_error_when_no_effective_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    # No snapshot saved — effective_at() will raise ScheduleVersionNotFound.

    result = dispatch_one(_run_decision(), schedule_store=ss, job_store=js)
    assert result.skipped is False
    assert result.run is None
    assert result.error is not None
    assert "No effective schedule snapshot" in result.error


def test_dispatch_one_error_when_schedule_absent_from_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    ss.save(
        (_daily_schedule(schedule_id="other"),),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    # Decision references "report" but only "other" is in the snapshot.
    result = dispatch_one(
        _run_decision(schedule_id="report"), schedule_store=ss, job_store=js
    )
    assert result.skipped is False
    assert result.run is None
    assert result.error is not None
    assert "not found in snapshot" in result.error


# ---------------------------------------------------------------------------
# dispatch_due — batch
# ---------------------------------------------------------------------------


def test_dispatch_due_empty_batch(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    results = dispatch_due((), schedule_store=ss, job_store=js)
    assert results == ()


def test_dispatch_due_mixed_decisions(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    ss.save(
        (_daily_schedule(),),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    t1 = _OCCURRENCE
    t2 = _OCCURRENCE + timedelta(hours=9)  # 17:00 IST same day

    records = (
        _run_decision(scheduled_at=t1, decision=WakeDecision.RUN),
        _run_decision(scheduled_at=t2, decision=WakeDecision.SKIPPED_BY_POLICY),
    )
    results = dispatch_due(records, schedule_store=ss, job_store=js)

    assert len(results) == 2
    assert results[0].run is not None
    assert results[0].skipped is False
    assert results[1].skipped is True
    assert results[1].run is None


def test_dispatch_due_continues_after_error(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    ss.save(
        (_daily_schedule(),),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    # First record: missing schedule in snapshot (error).
    # Second record: valid (should succeed).
    t1 = _OCCURRENCE
    t2 = _OCCURRENCE + timedelta(minutes=30)

    bad = _run_decision(schedule_id="nonexistent", scheduled_at=t1)
    good = _run_decision(schedule_id="report", scheduled_at=t2)

    results = dispatch_due((bad, good), schedule_store=ss, job_store=js)

    assert len(results) == 2
    assert results[0].error is not None
    assert results[0].run is None
    assert results[1].error is None
    assert results[1].run is not None


def test_dispatch_due_idempotent_on_full_replay(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    ss.save(
        (_daily_schedule(),),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    records = (_run_decision(),)
    first = dispatch_due(records, schedule_store=ss, job_store=js)
    second = dispatch_due(records, schedule_store=ss, job_store=js)

    assert first[0].run is not None
    assert second[0].run is not None
    assert first[0].run.run_id == second[0].run.run_id


def test_dispatch_blocks_when_dependency_occurrence_is_missing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    morning = _daily_schedule(schedule_id="morning")
    evening = replace(
        _daily_schedule(schedule_id="evening"),
        dependencies=("morning",),
    )
    ss.save(
        (morning, evening),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    result = dispatch_one(
        _run_decision(schedule_id="evening"),
        schedule_store=ss,
        job_store=js,
    )

    assert result.run is None
    assert result.error is not None
    assert "no run occurrence" in result.error


def test_dispatch_blocks_until_dependency_succeeded(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    morning = _daily_schedule(schedule_id="morning")
    evening = replace(
        _daily_schedule(schedule_id="evening"),
        dependencies=("morning",),
    )
    ss.save(
        (morning, evening),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    morning_result = dispatch_one(
        _run_decision(schedule_id="morning"),
        schedule_store=ss,
        job_store=js,
    )
    assert morning_result.run is not None

    pending = dispatch_one(
        _run_decision(schedule_id="evening"),
        schedule_store=ss,
        job_store=js,
    )
    assert pending.run is None
    assert pending.error is not None
    assert "PENDING, not SUCCEEDED" in pending.error


def test_dispatch_due_multiple_schedules(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js = _make_stores(path)
    morning = _daily_schedule(schedule_id="morning", job_id="synthetic-report")
    evening = _daily_schedule(schedule_id="evening", job_id="synthetic-report")
    ss.save(
        (morning, evening),
        expected_version=0,
        effective_at=_OCCURRENCE - timedelta(hours=1),
    )

    t_morning = _OCCURRENCE
    t_evening = _OCCURRENCE + timedelta(hours=9)

    records = (
        _run_decision(schedule_id="morning", scheduled_at=t_morning),
        _run_decision(schedule_id="evening", scheduled_at=t_evening),
    )
    results = dispatch_due(records, schedule_store=ss, job_store=js)
    assert all(r.run is not None for r in results)
    assert results[0].run is not None and results[1].run is not None
    assert results[0].run.run_id != results[1].run.run_id
    # Both schedules have the same job_id ("synthetic-report") but different
    # schedule_ids ("morning", "evening") which produce different occurrence keys.
    assert results[0].run.job_id == "synthetic-report"
    assert results[1].run.job_id == "synthetic-report"
    assert results[0].run.occurrence_key != results[1].run.occurrence_key
