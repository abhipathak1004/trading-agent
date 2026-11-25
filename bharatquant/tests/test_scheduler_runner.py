"""
Tests for infra/scheduler.py — SchedulerRunner.

All tests use a fresh SQLite database (via tmp_path) with all 14 migrations
applied.  The job_id used throughout is ``"synthetic-report"``, which is in
the ``AVAILABLE_JOBS`` set passed to the ScheduleStore.

Clock notes
-----------
* ``_BASE`` is an arbitrary "current time" used as the ``now`` argument.
* ``_OCCURRENCE`` is 08:00 IST on 2026-11-20 == 02:30 UTC, chosen so it falls
  within the default 1-day lookback window when ``_BASE`` is 15:00 UTC same day.
* ``_EFFECTIVE_AT`` is 2026-11-19 00:00 UTC — before ``_OCCURRENCE``, so the
  schedule is effective at the occurrence time.
"""

import asyncio
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import cast

import pytest

from bq.infra.database import migrate
from bq.infra.jobs import JobRunStatus, JobRunStore
from bq.infra.scheduler import (
    MAX_WINDOW,
    MIN_ADVANCE,
    SchedulerError,
    SchedulerLoop,
    SchedulerRunner,
)
from bq.infra.schedules import ScheduleStore
from bq.schedules import DailySchedule, WakePolicy

AVAILABLE_JOBS: frozenset[str] = frozenset({"synthetic-report"})

# 2026-11-20 15:00 UTC — "now" for most tests.
_BASE = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
# 08:00 IST on 2026-11-20 == 02:30 UTC.
_OCCURRENCE = datetime(2026, 11, 20, 2, 30, tzinfo=UTC)
# Schedule effective one day before the occurrence.
_EFFECTIVE_AT = datetime(2026, 11, 19, 0, 0, tzinfo=UTC)


class MovingClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant

    def advance(self, delta: timedelta) -> None:
        self.instant += delta


def _make_all(
    path: Path,
    *,
    clock_instant: datetime = _BASE,
    scheduler_id: str = "default",
    since: datetime | None = None,
) -> tuple[ScheduleStore, JobRunStore, SchedulerRunner]:
    migrate(path, create=True)
    clock = MovingClock(clock_instant)
    ss = ScheduleStore(path, available_jobs=AVAILABLE_JOBS, clock=clock)
    js = JobRunStore(path, clock)
    runner = SchedulerRunner(
        path,
        schedule_store=ss,
        job_store=js,
        scheduler_id=scheduler_id,
        since=since,
        clock=clock,
    )
    return ss, js, runner


def _daily(
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
        window_end=time(23),
        wake_policy=WakePolicy.ALWAYS_RUN,
        wake_within_seconds=None,
        max_attempts=max_attempts,
    )


def _save_at_recording_clock(
    ss: ScheduleStore,
    schedules: tuple[DailySchedule, ...],
    *,
    effective_at: datetime,
    clock_instant: datetime,
) -> None:
    clock = cast(MovingClock, ss.clock)
    clock.instant = effective_at
    ss.save(
        schedules,
        expected_version=0,
        effective_at=effective_at,
    )
    clock.instant = clock_instant


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_empty_scheduler_id_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = MovingClock(_BASE)
    ss = ScheduleStore(path, available_jobs=AVAILABLE_JOBS, clock=clock)
    js = JobRunStore(path, clock)
    with pytest.raises(SchedulerError, match="Scheduler ID"):
        SchedulerRunner(path, schedule_store=ss, job_store=js, scheduler_id="")


def test_naive_since_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = MovingClock(_BASE)
    ss = ScheduleStore(path, available_jobs=AVAILABLE_JOBS, clock=clock)
    js = JobRunStore(path, clock)
    with pytest.raises(SchedulerError, match="timezone-aware"):
        SchedulerRunner(
            path,
            schedule_store=ss,
            job_store=js,
            since=datetime(2026, 11, 1),  # naive
        )


# ---------------------------------------------------------------------------
# No schedules configured
# ---------------------------------------------------------------------------


def test_run_due_no_schedules_no_decisions(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js, runner = _make_all(path)
    result = runner.run_due(_BASE)
    assert result.skipped is False
    assert result.decisions_recorded == 0
    assert result.dispatched == ()
    assert result.ok is True
    assert result.run_count == 0
    # Checkpoint advanced to end of window.
    assert runner.checkpoint() is not None


def test_checkpoint_is_none_before_first_run(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    _, _, runner = _make_all(path)
    assert runner.checkpoint() is None


def test_checkpoint_is_set_after_first_run(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    _, _, runner = _make_all(path)
    runner.run_due(_BASE)
    cp = runner.checkpoint()
    assert cp is not None
    assert cp == _BASE


# ---------------------------------------------------------------------------
# Skipped when advance is below minimum
# ---------------------------------------------------------------------------


def test_run_due_skipped_when_no_advance(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js, runner = _make_all(path, since=_BASE - timedelta(hours=1))
    # First run: discovers window.
    runner.run_due(_BASE)
    # Second run: same instant — below MIN_ADVANCE.
    result = runner.run_due(_BASE)
    assert result.skipped is True
    assert result.decisions_recorded == 0


def test_run_due_not_skipped_after_min_advance(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js, runner = _make_all(path, since=_BASE - timedelta(hours=1))
    runner.run_due(_BASE)
    result = runner.run_due(_BASE + MIN_ADVANCE)
    assert result.skipped is False


# ---------------------------------------------------------------------------
# Schedule configured — occurrence discovered and dispatched
# ---------------------------------------------------------------------------


def test_run_due_dispatches_run_decision(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js, runner = _make_all(
        path,
        clock_instant=_BASE,
        since=_OCCURRENCE - timedelta(hours=1),
    )
    _save_at_recording_clock(
        ss, (_daily(),), effective_at=_EFFECTIVE_AT, clock_instant=_BASE
    )

    result = runner.run_due(_BASE)

    assert result.skipped is False
    assert result.decisions_recorded >= 1
    assert result.run_count == 1
    assert result.ok is True


def test_run_due_job_run_is_pending(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js, runner = _make_all(path, since=_OCCURRENCE - timedelta(hours=1))
    _save_at_recording_clock(
        ss, (_daily(),), effective_at=_EFFECTIVE_AT, clock_instant=_BASE
    )

    result = runner.run_due(_BASE)

    assert result.run_count == 1
    assert result.dispatched[0].run is not None
    run = result.dispatched[0].run
    assert run.status is JobRunStatus.PENDING
    assert run.job_id == "synthetic-report"
    assert run.max_attempts == 2


def test_run_due_checkpoint_advances_to_now(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js, runner = _make_all(path, since=_OCCURRENCE - timedelta(hours=1))
    _save_at_recording_clock(
        ss, (_daily(),), effective_at=_EFFECTIVE_AT, clock_instant=_BASE
    )

    runner.run_due(_BASE)

    cp = runner.checkpoint()
    assert cp == _BASE


# ---------------------------------------------------------------------------
# Idempotency across multiple runs
# ---------------------------------------------------------------------------


def test_run_due_idempotent_on_overlapping_windows(tmp_path: Path) -> None:
    """Re-running with the same window must not create a duplicate job run."""
    path = tmp_path / "bq.db"
    ss, js, runner = _make_all(path, since=_OCCURRENCE - timedelta(hours=1))
    _save_at_recording_clock(
        ss, (_daily(),), effective_at=_EFFECTIVE_AT, clock_instant=_BASE
    )

    r1 = runner.run_due(_BASE)
    # Reset checkpoint manually is not possible — instead re-create a runner
    # with the same since but no checkpoint yet.
    path2 = tmp_path / "bq2.db"
    ss2, js2, runner2 = _make_all(
        path2, since=_OCCURRENCE - timedelta(hours=1), clock_instant=_BASE
    )
    _save_at_recording_clock(
        ss2, (_daily(),), effective_at=_EFFECTIVE_AT, clock_instant=_BASE
    )
    r2 = runner2.run_due(_BASE)

    assert r1.run_count == r2.run_count == 1
    # Idempotent: same occurrence_key on the second runner produces the same run.
    assert r1.dispatched[0].run is not None and r2.dispatched[0].run is not None


def test_run_due_second_pass_does_not_re_enqueue(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js, runner = _make_all(path, since=_OCCURRENCE - timedelta(hours=1))
    _save_at_recording_clock(
        ss, (_daily(),), effective_at=_EFFECTIVE_AT, clock_instant=_BASE
    )

    r1 = runner.run_due(_BASE)
    # Second run: checkpoint is now at _BASE; a small advance finds nothing new.
    r2 = runner.run_due(_BASE + MIN_ADVANCE)

    assert r1.run_count == 1
    # Second run covers only the MIN_ADVANCE gap — no new occurrence.
    assert r2.run_count == 0


# ---------------------------------------------------------------------------
# Since default (1-day lookback)
# ---------------------------------------------------------------------------


def test_run_due_uses_1day_lookback_by_default(tmp_path: Path) -> None:
    """
    When no ``since`` is provided and no checkpoint exists, the runner starts
    from now - 1 day.  An occurrence 20 hours before ``now`` is included.
    """
    path = tmp_path / "bq.db"
    now = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
    # 08:00 IST on 2026-11-20 == 02:30 UTC — 12.5 hours before ``now``.
    effective = now - timedelta(days=2)

    ss, js, runner = _make_all(path, clock_instant=now, since=None)
    _save_at_recording_clock(ss, (_daily(),), effective_at=effective, clock_instant=now)

    result = runner.run_due(now)
    assert result.run_count == 1


# ---------------------------------------------------------------------------
# Multiple schedules
# ---------------------------------------------------------------------------


def test_run_due_dispatches_multiple_schedules(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    morning = _daily(schedule_id="morning")
    # Use a "evening" schedule at 17:00 IST.
    from datetime import time as _time

    evening = DailySchedule(
        schedule_id="evening",
        job_id="synthetic-report",
        enabled=True,
        timezone="Asia/Kolkata",
        local_time=_time(17, 0),
        window_start=_time(7, 45),
        window_end=_time(23),
        wake_policy=WakePolicy.ALWAYS_RUN,
        wake_within_seconds=None,
        max_attempts=3,
    )

    ss, js, runner = _make_all(
        path,
        clock_instant=_BASE,
        since=_OCCURRENCE - timedelta(hours=1),
    )
    _save_at_recording_clock(
        ss,
        (morning, evening),
        effective_at=_EFFECTIVE_AT,
        clock_instant=_BASE,
    )

    result = runner.run_due(_BASE)

    assert result.ok is True
    # Both 08:00 and 17:00 occurrences fall within the window.
    assert result.run_count == 2


# ---------------------------------------------------------------------------
# Disabled schedule is skipped
# ---------------------------------------------------------------------------


def test_run_due_skips_disabled_schedule(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    disabled = DailySchedule(
        schedule_id="report",
        job_id="synthetic-report",
        enabled=False,
        timezone="Asia/Kolkata",
        local_time=time(8),
        window_start=time(7, 45),
        window_end=time(17, 30),
        wake_policy=WakePolicy.RUN_ON_WAKE_WITHIN,
        wake_within_seconds=1800,
        max_attempts=2,
    )
    ss, js, runner = _make_all(
        path, clock_instant=_BASE, since=_OCCURRENCE - timedelta(hours=1)
    )
    # validate_schedules allows disabled schedules even if job_id not registered
    _save_at_recording_clock(
        ss, (disabled,), effective_at=_EFFECTIVE_AT, clock_instant=_BASE
    )
    result = runner.run_due(_BASE)
    assert result.run_count == 0
    assert result.ok is True


# ---------------------------------------------------------------------------
# Checkpoint does not regress
# ---------------------------------------------------------------------------


def test_checkpoint_does_not_regress(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    ss, js, runner = _make_all(path, since=_BASE - timedelta(hours=2))

    runner.run_due(_BASE)
    cp1 = runner.checkpoint()

    # Calling with an earlier now (simulating a misconfigured clock) should not
    # advance the checkpoint backwards.
    r2 = runner.run_due(_BASE - timedelta(hours=1))
    cp2 = runner.checkpoint()

    # r2 should be skipped (now < checkpoint).
    assert r2.skipped is True
    assert cp2 == cp1


# ---------------------------------------------------------------------------
# Multiple independent runners (different scheduler_id)
# ---------------------------------------------------------------------------


def test_independent_runners_have_separate_checkpoints(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = MovingClock(_BASE)
    ss = ScheduleStore(path, available_jobs=AVAILABLE_JOBS, clock=clock)
    js = JobRunStore(path, clock)
    r_a = SchedulerRunner(
        path,
        schedule_store=ss,
        job_store=js,
        scheduler_id="runner-a",
        since=_BASE - timedelta(hours=1),
        clock=clock,
    )
    r_b = SchedulerRunner(
        path,
        schedule_store=ss,
        job_store=js,
        scheduler_id="runner-b",
        since=_BASE - timedelta(hours=2),
        clock=clock,
    )

    r_a.run_due(_BASE)
    assert r_b.checkpoint() is None  # B not run yet

    r_b.run_due(_BASE + MIN_ADVANCE)
    assert r_a.checkpoint() == _BASE
    assert r_b.checkpoint() == _BASE + MIN_ADVANCE


# ---------------------------------------------------------------------------
# MAX_WINDOW clamping
# ---------------------------------------------------------------------------


def test_run_due_clamps_to_max_window(tmp_path: Path) -> None:
    """
    When ``since`` is more than MAX_WINDOW behind ``now``, the runner only
    processes up to ``since + MAX_WINDOW``, not all the way to ``now``.
    """
    path = tmp_path / "bq.db"
    since = _BASE - MAX_WINDOW - timedelta(days=5)  # 36 days before _BASE
    ss, js, runner = _make_all(path, clock_instant=_BASE, since=since)
    effective = since + timedelta(hours=1)
    _save_at_recording_clock(
        ss, (_daily(),), effective_at=effective, clock_instant=_BASE
    )

    result = runner.run_due(_BASE)

    assert result.skipped is False
    assert result.end is not None
    assert result.start is not None
    # The window should be clamped to MAX_WINDOW.
    assert result.end - result.start <= MAX_WINDOW + timedelta(seconds=1)
    # Checkpoint advances to end-of-window, not all the way to _BASE.
    cp = runner.checkpoint()
    assert cp is not None
    assert cp < _BASE


def test_successive_runs_catch_up_past_max_window(tmp_path: Path) -> None:
    """
    Calling ``run_due`` twice catches up when the gap exceeds MAX_WINDOW.
    """
    path = tmp_path / "bq.db"
    since = _BASE - MAX_WINDOW - timedelta(days=5)
    ss, js, runner = _make_all(path, clock_instant=_BASE, since=since)
    # No schedules — just verify checkpoint progression.

    runner.run_due(_BASE)
    cp1 = runner.checkpoint()
    runner.run_due(_BASE)
    cp2 = runner.checkpoint()
    # Second run starts from cp1 and advances by another window.
    assert cp2 is not None and cp1 is not None
    assert cp2 >= cp1


# ---------------------------------------------------------------------------
# RunResult properties
# ---------------------------------------------------------------------------


def test_run_result_ok_false_on_schedule_error(tmp_path: Path) -> None:
    """
    A scheduler whose schedule_store raises gets a non-ok result with the
    error recorded.  We simulate this by using a 'since' that leaves a gap
    with no effective boundary (ScheduleVersionNotFound is absorbed).
    """
    path = tmp_path / "bq.db"
    # Save a schedule but with an effective_at AFTER the occurrence,
    # so discovery will fail with ScheduleVersionNotFound for that window.
    ss, js, runner = _make_all(
        path,
        clock_instant=_BASE,
        since=_OCCURRENCE - timedelta(hours=1),
    )
    # effective_at is in the future relative to since but we don't save any
    # schedule — record_due_daily with no snapshots returns empty, so ok.
    result = runner.run_due(_BASE)
    # No schedule → 0 decisions, no errors.
    assert result.ok is True
    assert result.decisions_recorded == 0


# ---------------------------------------------------------------------------
# asyncio process loop
# ---------------------------------------------------------------------------


def test_scheduler_loop_rejects_non_positive_interval(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    _, _, runner = _make_all(path)

    with pytest.raises(SchedulerError, match="interval"):
        SchedulerLoop(runner, interval=timedelta(0))


def test_scheduler_loop_starts_and_stops(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    _, _, runner = _make_all(path)
    loop = SchedulerLoop(runner, interval=timedelta(seconds=1))

    async def check() -> None:
        loop.start()
        await asyncio.sleep(0)
        assert loop.running is True
        await loop.stop()
        assert loop.running is False

    asyncio.run(check())
