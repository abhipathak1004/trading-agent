"""
Scheduler runner: discovers due occurrences, records wake decisions, and
dispatches RUN decisions to the job store.

This is the automatic trigger layer that sits above ``infra/dispatch.py``.
It is called periodically (from an asyncio task, a launchd job, or a test)
and advances a durable high-water mark in ``scheduler_checkpoints``.

Design rules
------------
* **At-least-once** guarantee: on restart the runner re-processes from the
  last persisted checkpoint.  ``dispatch_one`` is idempotent for matching
  parameters so re-enqueuing an already-enqueued occurrence is safe.
* **Maximum window**: a single pass covers at most ``MAX_WINDOW`` (31 days).
  Gaps larger than this are split by advancing the checkpoint in steps.
  In practice the runner should be called often enough that the window is
  minutes, not days.
* **Clock ordering**: the ``dispatched_through`` mark only advances.  Calls
  that arrive while the clock appears to have moved backwards are skipped
  (returns a ``RunResult`` with ``skipped=True``).
* **No dependency ordering**: this runner dispatches all enabled schedules
  independently.  Dependency-aware dispatch is deferred.
* **No catch-up safety**: a late ``RUN`` decision is dispatched without
  checking whether the job should still run.  Job handlers must decide.
* **No automatic retry**: dispatch errors are recorded in ``RunResult``; the
  caller is responsible for alerting or re-invoking.

What this does NOT do
---------------------
* Does not execute job handlers.
* Does not filter by calendar, instrument eligibility or trading day.
* Does not validate dependencies between schedules.
* Does not use asyncio internally — it is synchronous and thread-safe.
"""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bq.core import Clock, SystemClock
from bq.infra.dispatch import DispatchResult, dispatch_due
from bq.infra.jobs import JobRunStore
from bq.infra.schedules import ScheduleStore, ScheduleVersionNotFound
from bq.schedules import ScheduleError, WakeDecision

# Hard upper bound on the discovery window per pass to match
# ScheduleStore.record_due_daily's 31-day limit.
MAX_WINDOW: timedelta = timedelta(days=31)

# Minimum forward progress per pass.  Calls that move the clock by less than
# this are skipped to avoid tight-loop hammering of the schedule store.
MIN_ADVANCE: timedelta = timedelta(seconds=1)

SCHEDULER_ID_MAX_LEN = 128


class SchedulerError(RuntimeError):
    """Raised for configuration or state errors in the scheduler runner."""


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise SchedulerError("Scheduler timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _ts(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _validate_scheduler_id(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > SCHEDULER_ID_MAX_LEN:
        raise SchedulerError(
            f"Scheduler ID must be a non-empty string of at most "
            f"{SCHEDULER_ID_MAX_LEN} characters."
        )


@dataclass(frozen=True, slots=True)
class RunResult:
    """
    Summary of one ``SchedulerRunner.run_due`` call.

    Attributes
    ----------
    skipped:
        ``True`` when the call was a no-op because ``now`` did not advance
        past the minimum threshold or was before the last checkpoint.
    start:
        The inclusive start of the processed window (last checkpoint, or the
        ``since`` sentinel on first run).
    end:
        The inclusive end of the processed window (``now``).
    decisions_recorded:
        Total wake decisions recorded in this pass (all outcomes).
    dispatched:
        Results for each ``WakeDecision.RUN`` decision that was dispatched.
    errors:
        Dispatch results with a non-``None`` ``error`` field.
    schedule_errors:
        Descriptions of schedule-discovery errors that prevented a schedule
        from being processed (e.g. version gaps, DST ambiguity).
    """

    skipped: bool
    start: datetime | None
    end: datetime | None
    decisions_recorded: int
    dispatched: tuple[DispatchResult, ...]
    errors: tuple[DispatchResult, ...]
    schedule_errors: tuple[str, ...]

    @property
    def run_count(self) -> int:
        """Number of job runs successfully enqueued."""
        return sum(1 for r in self.dispatched if r.run is not None)

    @property
    def error_count(self) -> int:
        return len(self.errors) + len(self.schedule_errors)

    @property
    def ok(self) -> bool:
        """``True`` iff no dispatch or schedule errors occurred."""
        return self.error_count == 0


_SKIPPED = RunResult(
    skipped=True,
    start=None,
    end=None,
    decisions_recorded=0,
    dispatched=(),
    errors=(),
    schedule_errors=(),
)


class SchedulerRunner:
    """
    Discovers due schedule occurrences, records wake decisions, dispatches
    ``RUN`` decisions to the job store, and advances a durable checkpoint.

    Parameters
    ----------
    path:
        Path to the SQLite database file.
    schedule_store:
        ``ScheduleStore`` instance for the same database.
    job_store:
        ``JobRunStore`` instance for the same database.
    scheduler_id:
        Stable identifier for this runner (default ``"default"``).  Use
        separate IDs for independent scheduler instances operating on the
        same database.
    since:
        On first run (no checkpoint in the database), discoveries start from
        this instant.  Defaults to one day before the first ``run_due`` call.
        Must be timezone-aware.  Ignored after the first checkpoint is saved.
    clock:
        Injected clock; defaults to ``SystemClock``.
    """

    def __init__(
        self,
        path: Path,
        *,
        schedule_store: ScheduleStore,
        job_store: JobRunStore,
        scheduler_id: str = "default",
        since: datetime | None = None,
        clock: Clock | None = None,
    ) -> None:
        _validate_scheduler_id(scheduler_id)
        if since is not None:
            _utc(since)
        self.path = path
        self.schedule_store = schedule_store
        self.job_store = job_store
        self.scheduler_id = scheduler_id
        self.since = since
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_due(self, now: datetime | None = None) -> RunResult:
        """
        Execute one scheduler pass up to ``now`` (default: current clock).

        Steps:
        1. Load the last checkpoint (or use ``since`` / now-1day as start).
        2. If the window is below ``MIN_ADVANCE``, skip and return early.
        3. Clamp the window to ``MAX_WINDOW`` (runs re-process until caught up).
        4. Call ``ScheduleStore.record_due_daily`` for the window.
        5. Dispatch all ``RUN`` decisions via ``dispatch_due``.
        6. Advance the checkpoint to the end of the processed window.
        7. Return a ``RunResult`` summary.
        """
        effective_now = _utc(self.clock.now() if now is None else now)
        start = self._load_checkpoint(effective_now)

        if effective_now - start < MIN_ADVANCE:
            return _SKIPPED

        end = min(effective_now, start + MAX_WINDOW)

        schedule_errors: list[str] = []
        decisions: list = []
        try:
            decisions = list(self.schedule_store.record_due_daily(start=start, end=end))
        except (ScheduleError, ScheduleVersionNotFound) as exc:
            schedule_errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            schedule_errors.append(f"Unexpected error during discovery: {exc}")

        # Only dispatch the RUN decisions.
        run_decisions = tuple(d for d in decisions if d.decision is WakeDecision.RUN)
        dispatch_results: tuple[DispatchResult, ...] = ()
        if run_decisions:
            dispatch_results = dispatch_due(
                run_decisions,
                schedule_store=self.schedule_store,
                job_store=self.job_store,
            )

        self._save_checkpoint(end)

        errors = tuple(r for r in dispatch_results if r.error is not None)
        return RunResult(
            skipped=False,
            start=start,
            end=end,
            decisions_recorded=len(decisions),
            dispatched=dispatch_results,
            errors=errors,
            schedule_errors=tuple(schedule_errors),
        )

    def checkpoint(self) -> datetime | None:
        """Return the current persisted checkpoint, or ``None`` if first run."""
        with closing(self._connect()) as connection:
            return self._read_checkpoint(connection)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _load_checkpoint(self, now: datetime) -> datetime:
        """Return the start of the next discovery window."""
        with closing(self._connect()) as connection:
            existing = self._read_checkpoint(connection)
        if existing is not None:
            return existing
        # First run: start from the caller-supplied ``since``, or one day back.
        return _utc(self.since) if self.since is not None else now - timedelta(days=1)

    def _read_checkpoint(self, connection: sqlite3.Connection) -> datetime | None:
        row = connection.execute(
            "SELECT dispatched_through FROM scheduler_checkpoints "
            "WHERE scheduler_id = ?",
            (self.scheduler_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_ts(str(row[0]))

    def _save_checkpoint(self, through: datetime) -> None:
        through_text = _ts(through)
        recorded_text = _ts(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT dispatched_through FROM scheduler_checkpoints "
                    "WHERE scheduler_id = ?",
                    (self.scheduler_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO scheduler_checkpoints "
                        "(scheduler_id, dispatched_through, recorded_at) "
                        "VALUES (?, ?, ?)",
                        (self.scheduler_id, through_text, recorded_text),
                    )
                else:
                    # Only advance; never move backwards.
                    if through_text > str(existing[0]):
                        connection.execute(
                            "UPDATE scheduler_checkpoints "
                            "SET dispatched_through = ?, recorded_at = ? "
                            "WHERE scheduler_id = ?",
                            (through_text, recorded_text, self.scheduler_id),
                        )


class SchedulerLoop:
    """Small asyncio process loop for the synchronous SchedulerRunner."""

    def __init__(
        self,
        runner: SchedulerRunner,
        *,
        interval: timedelta = timedelta(seconds=30),
    ) -> None:
        if interval <= timedelta(0):
            raise SchedulerError("Scheduler loop interval must be positive.")
        self.runner = runner
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self.last_result: RunResult | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._stop = None

    async def _run(self) -> None:
        stop = self._stop
        if stop is None:
            return
        while not stop.is_set():
            try:
                self.last_result = await asyncio.to_thread(self.runner.run_due)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # The runner records schedule and dispatch failures. An
                # unexpected loop failure is bounded and retried on the next tick.
                self.last_result = RunResult(
                    skipped=False,
                    start=None,
                    end=None,
                    decisions_recorded=0,
                    dispatched=(),
                    errors=(),
                    schedule_errors=(f"Unexpected scheduler loop error: {exc}",),
                )
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.interval.total_seconds()
                )
            except TimeoutError:
                continue
