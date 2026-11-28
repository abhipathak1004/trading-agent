from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from graphlib import CycleError, TopologicalSorter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bq.core import IDENTIFIER

EVERY_DAY = (0, 1, 2, 3, 4, 5, 6)


class ScheduleError(ValueError):
    pass


class WakePolicy(StrEnum):
    RUN_ON_WAKE_WITHIN = "RUN_ON_WAKE_WITHIN"
    SKIP = "SKIP"
    ALWAYS_RUN = "ALWAYS_RUN"


class WakeDecision(StrEnum):
    RUN = "RUN"
    SKIPPED_BY_POLICY = "SKIPPED_BY_POLICY"
    WAKE_WINDOW_EXPIRED = "WAKE_WINDOW_EXPIRED"
    OUTSIDE_OPERATING_WINDOW = "OUTSIDE_OPERATING_WINDOW"


def _validate_time(value: time, label: str) -> None:
    if not isinstance(value, time) or value.tzinfo is not None:
        raise ScheduleError(f"{label} must be a timezone-naive local time.")
    if value.second or value.microsecond or value.fold:
        raise ScheduleError(f"{label} must have whole-minute precision.")


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"Unknown schedule timezone: {name}.") from exc


@dataclass(frozen=True, slots=True)
class DailySchedule:
    schedule_id: str
    job_id: str
    enabled: bool
    timezone: str
    local_time: time
    window_start: time
    window_end: time
    wake_policy: WakePolicy
    wake_within_seconds: int | None
    max_attempts: int
    dependencies: tuple[str, ...] = ()
    weekdays: tuple[int, ...] = EVERY_DAY

    def __post_init__(self) -> None:
        if IDENTIFIER.fullmatch(self.schedule_id) is None:
            raise ScheduleError("Invalid schedule identifier.")
        if IDENTIFIER.fullmatch(self.job_id) is None:
            raise ScheduleError("Invalid scheduled job identifier.")
        if not isinstance(self.enabled, bool):
            raise ScheduleError("Schedule enabled must be a boolean.")
        _timezone(self.timezone)
        _validate_time(self.local_time, "Scheduled time")
        _validate_time(self.window_start, "Window start")
        _validate_time(self.window_end, "Window end")
        if not self.window_start <= self.local_time <= self.window_end:
            raise ScheduleError("Scheduled time must be inside the operating window.")
        if not isinstance(self.wake_policy, WakePolicy):
            raise ScheduleError("Invalid wake policy.")
        if self.wake_policy is WakePolicy.RUN_ON_WAKE_WITHIN:
            if (
                isinstance(self.wake_within_seconds, bool)
                or not isinstance(self.wake_within_seconds, int)
                or self.wake_within_seconds <= 0
            ):
                raise ScheduleError("Wake window must be a positive number of seconds.")
        elif self.wake_within_seconds is not None:
            raise ScheduleError("A wake window is only valid for RUN_ON_WAKE_WITHIN.")
        if isinstance(self.max_attempts, bool) or not isinstance(
            self.max_attempts, int
        ):
            raise ScheduleError("Maximum attempts must be an integer.")
        if self.max_attempts <= 0:
            raise ScheduleError("Maximum attempts must be positive.")
        if not isinstance(self.dependencies, tuple) or any(
            not isinstance(item, str) or IDENTIFIER.fullmatch(item) is None
            for item in self.dependencies
        ):
            raise ScheduleError("Invalid schedule dependency.")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ScheduleError("Schedule dependencies must not contain duplicates.")
        if (
            not isinstance(self.weekdays, tuple)
            or not self.weekdays
            or any(
                isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6
                for day in self.weekdays
            )
            or tuple(sorted(set(self.weekdays))) != self.weekdays
        ):
            raise ScheduleError(
                "Schedule weekdays must be distinct ordered days from 0 to 6."
            )


@dataclass(frozen=True, slots=True)
class IntervalSchedule(DailySchedule):
    interval_minutes: int = 1

    def __post_init__(self) -> None:
        DailySchedule.__post_init__(self)
        if (
            isinstance(self.interval_minutes, bool)
            or not isinstance(self.interval_minutes, int)
            or not 1 <= self.interval_minutes <= 1440
        ):
            raise ScheduleError("Interval must be between 1 and 1440 whole minutes.")


def validate_schedules(
    schedules: tuple[DailySchedule, ...], *, available_jobs: frozenset[str]
) -> None:
    by_id = {schedule.schedule_id: schedule for schedule in schedules}
    if len(by_id) != len(schedules):
        raise ScheduleError("Schedule identifiers must be unique.")
    for schedule in schedules:
        if schedule.enabled and schedule.job_id not in available_jobs:
            raise ScheduleError(f"Job {schedule.job_id} is not implemented.")
        for dependency in schedule.dependencies:
            if dependency not in by_id:
                raise ScheduleError(f"Missing schedule dependency: {dependency}.")
            if schedule.enabled and not by_id[dependency].enabled:
                raise ScheduleError(f"Schedule dependency {dependency} is disabled.")

    graph = {schedule.schedule_id: schedule.dependencies for schedule in schedules}
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ScheduleError("Schedule dependencies contain a cycle.") from exc


def _local_candidates(local: datetime, zone: ZoneInfo) -> set[datetime]:
    return {
        candidate.astimezone(UTC)
        for fold in (0, 1)
        if (candidate := local.replace(tzinfo=zone, fold=fold))
        .astimezone(UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        == local
    }


def decide_wake(
    schedule: DailySchedule, *, scheduled_at: datetime, woke_at: datetime
) -> WakeDecision:
    if not schedule.enabled:
        raise ScheduleError("Cannot evaluate a disabled schedule.")
    if scheduled_at.utcoffset() is None or woke_at.utcoffset() is None:
        raise ScheduleError("Wake timestamps must be timezone-aware.")
    scheduled = scheduled_at.astimezone(UTC)
    wake = woke_at.astimezone(UTC)
    if wake < scheduled:
        raise ScheduleError("Wake time cannot be before the scheduled occurrence.")
    zone = _timezone(schedule.timezone)
    local_scheduled = scheduled.astimezone(zone).replace(tzinfo=None)
    if local_scheduled.weekday() not in schedule.weekdays:
        raise ScheduleError("Occurrence does not match the scheduled weekdays.")
    if isinstance(schedule, IntervalSchedule):
        first = datetime.combine(local_scheduled.date(), schedule.local_time)
        elapsed = local_scheduled - first
        if (
            local_scheduled.time() > schedule.window_end
            or elapsed < timedelta(0)
            or elapsed % timedelta(minutes=schedule.interval_minutes) != timedelta(0)
        ):
            raise ScheduleError("Occurrence does not match the scheduled interval.")
    elif local_scheduled.time() != schedule.local_time:
        raise ScheduleError("Occurrence does not match the scheduled local time.")
    if len(_local_candidates(local_scheduled, zone)) != 1:
        raise ScheduleError(
            f"Scheduled local time is ambiguous or nonexistent on {local_scheduled.date()}."
        )
    local_wake_time = wake.astimezone(zone).time()
    if not schedule.window_start <= local_wake_time <= schedule.window_end:
        return WakeDecision.OUTSIDE_OPERATING_WINDOW
    if wake == scheduled:
        return WakeDecision.RUN
    if schedule.wake_policy is WakePolicy.SKIP:
        return WakeDecision.SKIPPED_BY_POLICY
    if schedule.wake_policy is WakePolicy.RUN_ON_WAKE_WITHIN:
        assert schedule.wake_within_seconds is not None
        elapsed_microseconds = (wake - scheduled) // timedelta(microseconds=1)
        if elapsed_microseconds > schedule.wake_within_seconds * 1_000_000:
            return WakeDecision.WAKE_WINDOW_EXPIRED
    return WakeDecision.RUN


def next_daily_occurrences(
    schedule: DailySchedule, *, after: datetime, count: int = 5
) -> tuple[datetime, ...]:
    if isinstance(schedule, IntervalSchedule):
        raise ScheduleError("Use interval preview for an interval schedule.")
    if not schedule.enabled:
        raise ScheduleError("Cannot preview a disabled schedule.")
    if after.utcoffset() is None:
        raise ScheduleError("Preview time must be timezone-aware.")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ScheduleError("Preview count must be between 1 and 100.")
    instant = after.astimezone(UTC)
    zone = _timezone(schedule.timezone)
    current_date = instant.astimezone(zone).date()
    result: list[datetime] = []
    while len(result) < count:
        if current_date.weekday() not in schedule.weekdays:
            if current_date == date.max:
                raise ScheduleError("Preview is outside the supported timezone.")
            current_date += timedelta(days=1)
            continue
        local = datetime.combine(current_date, schedule.local_time)
        candidates = _local_candidates(local, zone)
        if len(candidates) != 1:
            if (
                max(
                    local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                    for fold in (0, 1)
                )
                <= instant
            ):
                if current_date == date.max:
                    raise ScheduleError("Preview is outside the supported timezone.")
                current_date += timedelta(days=1)
                continue
            raise ScheduleError(
                f"Scheduled local time is ambiguous or nonexistent on {current_date}."
            )
        occurrence = candidates.pop()
        if occurrence > instant:
            result.append(occurrence)
        if len(result) < count:
            if current_date == date.max:
                raise ScheduleError("Preview is outside the supported timezone.")
            current_date += timedelta(days=1)
    return tuple(result)


def _interval_local_times(
    schedule: IntervalSchedule, day: date
) -> tuple[datetime, ...]:
    first = datetime.combine(day, schedule.local_time)
    last = datetime.combine(day, schedule.window_end)
    interval = timedelta(minutes=schedule.interval_minutes)
    return tuple(
        first + interval * index for index in range((last - first) // interval + 1)
    )


def next_interval_occurrences(
    schedule: IntervalSchedule, *, after: datetime, count: int = 5
) -> tuple[datetime, ...]:
    if not isinstance(schedule, IntervalSchedule):
        raise ScheduleError("Interval preview requires an interval schedule.")
    if not schedule.enabled:
        raise ScheduleError("Cannot preview a disabled schedule.")
    if not isinstance(after, datetime) or after.utcoffset() is None:
        raise ScheduleError("Preview time must be timezone-aware.")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ScheduleError("Preview count must be between 1 and 100.")
    instant = after.astimezone(UTC)
    zone = _timezone(schedule.timezone)
    current_date = instant.astimezone(zone).date()
    result: list[datetime] = []
    try:
        while len(result) < count:
            local_times = (
                _interval_local_times(schedule, current_date)
                if current_date.weekday() in schedule.weekdays
                else ()
            )
            for local in local_times:
                candidates = _local_candidates(local, zone)
                if len(candidates) != 1:
                    projections = tuple(
                        local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                        for fold in (0, 1)
                    )
                    if max(projections) > instant:
                        raise ScheduleError(
                            f"Scheduled local time is ambiguous or nonexistent on {current_date}."
                        )
                    continue
                occurrence = candidates.pop()
                if occurrence > instant:
                    result.append(occurrence)
                    if len(result) == count:
                        break
            if len(result) < count:
                current_date += timedelta(days=1)
    except OverflowError as exc:
        raise ScheduleError("Preview is outside the supported timezone.") from exc
    return tuple(result)


def interval_occurrences_between(
    schedule: IntervalSchedule, *, start: datetime, end: datetime, limit: int = 1000
) -> tuple[datetime, ...]:
    if not isinstance(schedule, IntervalSchedule):
        raise ScheduleError("Interval discovery requires an interval schedule.")
    if not schedule.enabled:
        raise ScheduleError("Cannot discover occurrences for a disabled schedule.")
    if (
        not isinstance(start, datetime)
        or not isinstance(end, datetime)
        or start.utcoffset() is None
        or end.utcoffset() is None
    ):
        raise ScheduleError("Discovery timestamps must be timezone-aware.")
    start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
    if end_utc < start_utc or end_utc - start_utc > timedelta(days=31):
        raise ScheduleError(
            "Discovery range must be ordered and no longer than 31 days."
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ScheduleError("Discovery limit must be between 1 and 1000.")
    zone = _timezone(schedule.timezone)
    result: list[datetime] = []
    try:
        start_date = start_utc.astimezone(zone).date()
        end_date = end_utc.astimezone(zone).date()
        current_date = date.fromordinal(
            max(1, min(start_date.toordinal(), end_date.toordinal()) - 2)
        )
        last_date = date.fromordinal(
            min(
                date.max.toordinal(),
                max(start_date.toordinal(), end_date.toordinal()) + 2,
            )
        )
        while current_date <= last_date:
            local_times = (
                _interval_local_times(schedule, current_date)
                if current_date.weekday() in schedule.weekdays
                else ()
            )
            for local in local_times:
                candidates = _local_candidates(local, zone)
                if len(candidates) != 1:
                    projections = tuple(
                        local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                        for fold in (0, 1)
                    )
                    if max(projections) >= start_utc and min(projections) <= end_utc:
                        raise ScheduleError(
                            f"Scheduled local time is ambiguous or nonexistent on {current_date}."
                        )
                else:
                    occurrence = candidates.pop()
                    if start_utc <= occurrence <= end_utc:
                        result.append(occurrence)
                        if len(result) > limit:
                            raise ScheduleError(
                                "Discovery exceeds the occurrence limit."
                            )
            if current_date == last_date:
                break
            current_date += timedelta(days=1)
    except OverflowError as exc:
        raise ScheduleError(
            "Discovery range is outside the supported timezone."
        ) from exc
    return tuple(sorted(result))


def daily_occurrences_between(
    schedule: DailySchedule, *, start: datetime, end: datetime
) -> tuple[datetime, ...]:
    if isinstance(schedule, IntervalSchedule):
        raise ScheduleError("Use interval discovery for an interval schedule.")
    if not schedule.enabled:
        raise ScheduleError("Cannot discover occurrences for a disabled schedule.")
    if (
        not isinstance(start, datetime)
        or not isinstance(end, datetime)
        or start.utcoffset() is None
        or end.utcoffset() is None
    ):
        raise ScheduleError("Discovery timestamps must be timezone-aware.")
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    if end_utc < start_utc or end_utc - start_utc > timedelta(days=31):
        raise ScheduleError(
            "Discovery range must be ordered and no longer than 31 days."
        )
    zone = _timezone(schedule.timezone)
    try:
        start_date = start_utc.astimezone(zone).date()
        end_date = end_utc.astimezone(zone).date()
    except OverflowError as exc:
        raise ScheduleError(
            "Discovery range is outside the supported timezone."
        ) from exc
    current_date = date.fromordinal(
        max(1, min(start_date.toordinal(), end_date.toordinal()) - 2)
    )
    last_date = date.fromordinal(
        min(date.max.toordinal(), max(start_date.toordinal(), end_date.toordinal()) + 2)
    )
    result: list[datetime] = []
    while current_date <= last_date:
        if current_date.weekday() not in schedule.weekdays:
            if current_date == last_date:
                break
            current_date += timedelta(days=1)
            continue
        local = datetime.combine(current_date, schedule.local_time)
        try:
            candidates = _local_candidates(local, zone)
        except OverflowError as exc:
            raise ScheduleError(
                "Discovery range is outside the supported timezone."
            ) from exc
        if len(candidates) != 1:
            projections = tuple(
                local.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)
            )
            if max(projections) >= start_utc and min(projections) <= end_utc:
                raise ScheduleError(
                    f"Scheduled local time is ambiguous or nonexistent on {current_date}."
                )
        else:
            occurrence = candidates.pop()
            if start_utc <= occurrence <= end_utc:
                result.append(occurrence)
        if current_date == last_date:
            break
        current_date += timedelta(days=1)
    return tuple(result)
