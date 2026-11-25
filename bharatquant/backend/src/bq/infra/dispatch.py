"""
Wake-to-job dispatch.

Converts ``WakeDecision.RUN`` records into durable ``JobRun`` occurrences.

This module sits between the schedule store (which records decisions) and
the job-run store (which tracks execution).  It is the **only** place that
converts a wake decision into an enqueued run; callers must not bypass it.

Constraints enforced here
--------------------------
* Only ``WakeDecision.RUN`` decisions are dispatched; all others are silently
  skipped.
* The occurrence key is ``"<schedule_id>:<scheduled_at_iso>"``, using the
  canonical UTC ISO-8601 representation produced by the schedule store.
  Idempotency is guaranteed by ``JobRunStore.enqueue``.
* ``job_id`` and ``max_attempts`` come from the schedule definition that was
  effective at the occurrence time; both must match across retry calls
  (``JobRunConflict`` is raised if they differ).
* The dispatch batch is **not** atomic across multiple decisions.  Each
  decision is enqueued individually so a partial failure leaves already-queued
  runs intact.  Callers should log and re-attempt only the failed ones.
* No calendar, dependency or catch-up logic is applied here.  Those checks
  belong to the job handler or a future dependency-aware dispatcher.

Not implemented here
---------------------
* Automatic triggering (the caller is responsible for discovering due
  occurrences and calling :func:`dispatch_due`).
* Dependency ordering between schedules.
* Catch-up safety (whether a late ``RUN`` should actually run is not checked).
* Job handler registration or execution.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from bq.infra.jobs import JobRun, JobRunConflict, JobRunStatus, JobRunStore
from bq.infra.schedules import (
    ScheduleStore,
    ScheduleVersionNotFound,
    WakeDecisionRecord,
)
from bq.schedules import DailySchedule, WakeDecision


class DispatchError(RuntimeError):
    """Raised when a decision cannot be dispatched."""


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of attempting to dispatch one wake decision."""

    record: WakeDecisionRecord
    run: JobRun | None
    skipped: bool
    error: str | None


def _occurrence_key(record: WakeDecisionRecord) -> str:
    """Canonical, stable occurrence key for a wake decision."""
    scheduled_text = (
        record.scheduled_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return f"{record.schedule_id}:{scheduled_text}"


def _dependency_key(schedule_id: str, scheduled_at: datetime) -> str:
    scheduled_text = (
        scheduled_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return f"{schedule_id}:{scheduled_text}"


def _effective_schedule(
    record: WakeDecisionRecord,
    schedule_store: ScheduleStore,
) -> DailySchedule:
    """
    Return the schedule definition effective at the occurrence time.

    Raises :exc:`DispatchError` if the snapshot cannot be found or the
    schedule_id is absent from that snapshot.
    """
    try:
        effective = schedule_store.effective_at(record.scheduled_at)
    except ScheduleVersionNotFound as exc:
        raise DispatchError(
            f"No effective schedule snapshot at {record.scheduled_at.isoformat()} "
            f"for schedule '{record.schedule_id}'."
        ) from exc

    schedule: DailySchedule | None = next(
        (
            s
            for s in effective.snapshot.schedules
            if s.schedule_id == record.schedule_id
        ),
        None,
    )
    if schedule is None:
        raise DispatchError(
            f"Schedule '{record.schedule_id}' not found in snapshot "
            f"version {effective.snapshot.version}."
        )
    return schedule


def _dependencies_succeeded(
    schedule: DailySchedule,
    record: WakeDecisionRecord,
    *,
    schedule_store: ScheduleStore,
    job_store: JobRunStore,
) -> str | None:
    """Return an error string if a dependency occurrence is not succeeded."""
    if not schedule.dependencies:
        return None
    try:
        effective = schedule_store.effective_at(record.scheduled_at)
    except ScheduleVersionNotFound:
        return f"No effective schedule snapshot at {record.scheduled_at.isoformat()}."

    for dependency_id in schedule.dependencies:
        dependency = next(
            (
                item
                for item in effective.snapshot.schedules
                if item.schedule_id == dependency_id
            ),
            None,
        )
        if dependency is None:
            return f"Dependency schedule '{dependency_id}' not found in snapshot."
        dependency_run = job_store.find(
            dependency.job_id,
            _dependency_key(dependency_id, record.scheduled_at),
        )
        if dependency_run is None:
            return f"Dependency schedule '{dependency_id}' has no run occurrence."
        if dependency_run.status is not JobRunStatus.SUCCEEDED:
            return (
                f"Dependency schedule '{dependency_id}' is "
                f"{dependency_run.status.value}, not SUCCEEDED."
            )
    return None


def dispatch_one(
    record: WakeDecisionRecord,
    *,
    schedule_store: ScheduleStore,
    job_store: JobRunStore,
) -> DispatchResult:
    """
    Attempt to dispatch a single wake decision.

    Returns a :class:`DispatchResult` describing whether the run was enqueued,
    skipped (non-RUN decision) or failed with an error message.

    Non-RUN decisions are skipped without raising.
    ``JobRunConflict`` with matching parameters is treated as idempotent success
    (the existing run is returned by ``JobRunStore.enqueue``).
    ``JobRunConflict`` with differing parameters is surfaced as an error.
    Any other exception is surfaced as an error without re-raising.
    """
    if record.decision is not WakeDecision.RUN:
        return DispatchResult(record=record, run=None, skipped=True, error=None)

    occurrence_key = _occurrence_key(record)
    try:
        schedule = _effective_schedule(record, schedule_store)
    except DispatchError as exc:
        return DispatchResult(record=record, run=None, skipped=False, error=str(exc))

    dependency_error = _dependencies_succeeded(
        schedule,
        record,
        schedule_store=schedule_store,
        job_store=job_store,
    )
    if dependency_error is not None:
        return DispatchResult(
            record=record,
            run=None,
            skipped=False,
            error=dependency_error,
        )

    try:
        run = job_store.enqueue(
            schedule.job_id,
            occurrence_key,
            scheduled_at=record.scheduled_at,
            max_attempts=schedule.max_attempts,
        )
    except JobRunConflict as exc:
        # Mismatched parameters on an existing occurrence is a real error.
        # Matching parameters: JobRunStore.enqueue returns the existing run
        # directly (no exception), so reaching this branch indicates a
        # job_id or max_attempts change between dispatch calls.
        return DispatchResult(record=record, run=None, skipped=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        return DispatchResult(record=record, run=None, skipped=False, error=str(exc))

    return DispatchResult(record=record, run=run, skipped=False, error=None)


def dispatch_due(
    records: tuple[WakeDecisionRecord, ...],
    *,
    schedule_store: ScheduleStore,
    job_store: JobRunStore,
) -> tuple[DispatchResult, ...]:
    """
    Dispatch a batch of wake decisions, returning one :class:`DispatchResult`
    per input record.

    Records are dispatched in order.  Errors in one record do not prevent
    subsequent records from being attempted.  The caller is responsible for
    inspecting results and re-attempting or alerting on failures.

    Only ``WakeDecision.RUN`` decisions are enqueued; the rest are skipped
    (``DispatchResult.skipped == True``).
    """
    return tuple(
        dispatch_one(record, schedule_store=schedule_store, job_store=job_store)
        for record in records
    )
