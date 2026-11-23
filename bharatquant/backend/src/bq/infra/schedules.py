import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal

from bq.core import Clock, SystemClock
from bq.infra.audit import append_event
from bq.schedules import (
    EVERY_DAY,
    IDENTIFIER,
    DailySchedule,
    IntervalSchedule,
    ScheduleError,
    WakeDecision,
    WakePolicy,
    daily_occurrences_between,
    decide_wake,
    interval_occurrences_between,
    validate_schedules,
)


class ScheduleConflict(RuntimeError):
    pass


class ScheduleVersionNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class ScheduleSnapshot:
    version: int
    schedules: tuple[DailySchedule, ...]


@dataclass(frozen=True, slots=True)
class EffectiveSchedule:
    snapshot: ScheduleSnapshot
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class WakeDecisionRecord:
    schedule_id: str
    scheduled_at: datetime
    observed_at: datetime
    schedule_version: int
    decision: WakeDecision


@dataclass(frozen=True, slots=True)
class ScheduleOverview:
    latest: ScheduleSnapshot
    effective: EffectiveSchedule | None
    effective_state: Literal["not_configured", "available", "unknown"]
    observed_at: datetime
    decision_count: int
    recent_decisions: tuple[WakeDecisionRecord, ...]


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ScheduleError("Schedule timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _serialize(schedules: tuple[DailySchedule, ...]) -> str:
    return json.dumps(
        [
            {
                "schedule_id": item.schedule_id,
                "job_id": item.job_id,
                "enabled": item.enabled,
                "timezone": item.timezone,
                "local_time": item.local_time.isoformat(timespec="minutes"),
                "window_start": item.window_start.isoformat(timespec="minutes"),
                "window_end": item.window_end.isoformat(timespec="minutes"),
                "wake_policy": item.wake_policy.value,
                "wake_within_seconds": item.wake_within_seconds,
                "max_attempts": item.max_attempts,
                "dependencies": list(item.dependencies),
                **(
                    {"weekdays": list(item.weekdays)}
                    if item.weekdays != EVERY_DAY
                    else {}
                ),
                **(
                    {"interval_minutes": item.interval_minutes}
                    if isinstance(item, IntervalSchedule)
                    else {}
                ),
            }
            for item in sorted(schedules, key=lambda value: value.schedule_id)
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _deserialize(payload: str) -> tuple[DailySchedule, ...]:
    try:
        data = json.loads(payload)
        if not isinstance(data, list):
            raise ValueError("Snapshot must be a list.")
        schedules_list: list[DailySchedule] = []
        for item in data:
            daily = DailySchedule(
                schedule_id=item["schedule_id"],
                job_id=item["job_id"],
                enabled=item["enabled"],
                timezone=item["timezone"],
                local_time=time.fromisoformat(item["local_time"]),
                window_start=time.fromisoformat(item["window_start"]),
                window_end=time.fromisoformat(item["window_end"]),
                wake_policy=WakePolicy(item["wake_policy"]),
                wake_within_seconds=item["wake_within_seconds"],
                max_attempts=item["max_attempts"],
                dependencies=tuple(item["dependencies"]),
                weekdays=tuple(item.get("weekdays", EVERY_DAY)),
            )
            if "interval_minutes" in item:
                schedules_list.append(
                    IntervalSchedule(
                        schedule_id=daily.schedule_id,
                        job_id=daily.job_id,
                        enabled=daily.enabled,
                        timezone=daily.timezone,
                        local_time=daily.local_time,
                        window_start=daily.window_start,
                        window_end=daily.window_end,
                        wake_policy=daily.wake_policy,
                        wake_within_seconds=daily.wake_within_seconds,
                        max_attempts=daily.max_attempts,
                        dependencies=daily.dependencies,
                        weekdays=daily.weekdays,
                        interval_minutes=item["interval_minutes"],
                    )
                )
            else:
                schedules_list.append(daily)
        schedules = tuple(schedules_list)
        if _serialize(schedules) != payload:
            raise ValueError("Snapshot is not canonical.")
        validate_schedules(
            schedules,
            available_jobs=frozenset(item.job_id for item in schedules if item.enabled),
        )
        return schedules
    except (KeyError, TypeError, ValueError) as exc:
        raise sqlite3.DatabaseError("Invalid stored schedule snapshot.") from exc


class ScheduleStore:
    def __init__(
        self, path: Path, *, available_jobs: frozenset[str], clock: Clock | None = None
    ) -> None:
        self.path = path
        self.available_jobs = available_jobs
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def load(self, version: int | None = None) -> ScheduleSnapshot:
        if version is not None and (
            isinstance(version, bool) or not isinstance(version, int) or version < 1
        ):
            raise ScheduleError("Schedule version must be a positive integer.")
        with closing(self._connect()) as connection:
            return self._load(connection, version=version)

    def save(
        self,
        schedules: tuple[DailySchedule, ...],
        *,
        expected_version: int,
        effective_at: datetime | None = None,
    ) -> ScheduleSnapshot:
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ScheduleError(
                "Expected schedule version must be a nonnegative integer."
            )
        validate_schedules(schedules, available_jobs=self.available_jobs)
        if effective_at is not None:
            _utc(effective_at)
        payload = _serialize(schedules)
        digest = sha256(payload.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                previous = self._load(connection)
                if previous.version != expected_version:
                    raise ScheduleConflict(
                        "Schedule version changed; reload before saving."
                    )
                now = _utc(self.clock.now())
                recorded_text = _timestamp(now)
                effective_text = _timestamp(
                    effective_at if effective_at is not None else now
                )
                if effective_text < recorded_text:
                    raise ScheduleError(
                        "Schedule effective time cannot precede recording."
                    )
                last = connection.execute(
                    "SELECT recorded_at FROM schedule_versions WHERE version = ?",
                    (previous.version,),
                ).fetchone()
                if last is not None and recorded_text < last[0]:
                    raise ScheduleError(
                        "Schedule recording clock cannot move backwards."
                    )
                last_boundary = connection.execute(
                    "SELECT MAX(effective_at) FROM schedule_effective_boundaries"
                ).fetchone()[0]
                if last_boundary is not None and effective_text <= last_boundary:
                    raise ScheduleError("Schedule effective time must advance.")
                version = previous.version + 1
                connection.execute(
                    "INSERT INTO schedule_versions "
                    "(version, recorded_at, snapshot_json, snapshot_hash) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        version,
                        recorded_text,
                        payload,
                        digest,
                    ),
                )
                connection.execute(
                    "INSERT INTO schedule_effective_boundaries (version, effective_at) VALUES (?, ?)",
                    (version, effective_text),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="schedule.snapshot.saved",
                    object_type="schedule_snapshot",
                    object_id=str(version),
                    config_version=str(version),
                    payload={"snapshot_hash": digest, "effective_at": effective_text},
                )
                return ScheduleSnapshot(version, _deserialize(payload))

    def effective_at(self, instant: datetime) -> EffectiveSchedule:
        instant_text = _timestamp(instant)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            return self._effective_at(connection, instant_text)

    def overview(self) -> ScheduleOverview:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            latest = self._load(connection)
            observed_at = _utc(self.clock.now())
            now = _timestamp(observed_at)
            effective: EffectiveSchedule | None = None
            state: Literal["not_configured", "available", "unknown"] = "not_configured"
            if latest.version:
                try:
                    effective = self._effective_at(connection, now)
                except ScheduleVersionNotFound:
                    state = "unknown"
                else:
                    state = "available"
            count = connection.execute(
                "SELECT count(*) FROM schedule_wake_decisions"
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT schedule_id, scheduled_at, observed_at, schedule_version, decision "
                "FROM schedule_wake_decisions "
                "ORDER BY observed_at DESC, schedule_id, scheduled_at DESC LIMIT 20"
            ).fetchall()
            return ScheduleOverview(
                latest,
                effective,
                state,
                observed_at,
                int(count),
                tuple(_decode_wake_decision(row) for row in rows),
            )

    def record_wake_decision(
        self, schedule_id: str, *, scheduled_at: datetime
    ) -> WakeDecisionRecord:
        if (
            not isinstance(schedule_id, str)
            or IDENTIFIER.fullmatch(schedule_id) is None
        ):
            raise ScheduleError("Invalid schedule identifier.")
        _utc(scheduled_at)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                return self._record_wake_decision(connection, schedule_id, scheduled_at)

    def record_due_daily(
        self, *, start: datetime, end: datetime, limit: int = 500
    ) -> tuple[WakeDecisionRecord, ...]:
        start = _utc(start)
        end = _utc(end)
        if end < start or end - start > timedelta(days=31):
            raise ScheduleError(
                "Discovery range must be ordered and no longer than 31 days."
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ScheduleError("Discovery limit must be between 1 and 1000.")
        start_text, end_text = _timestamp(start), _timestamp(end)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                now = _utc(self.clock.now())
                if end > now:
                    raise ScheduleError("Discovery cannot include future occurrences.")
                first = self._effective_at(connection, start_text)
                uncertain = connection.execute(
                    "SELECT 1 FROM schedule_versions AS s "
                    "LEFT JOIN schedule_effective_boundaries AS b ON b.version = s.version "
                    "WHERE s.version > ? AND s.recorded_at <= ? AND b.version IS NULL LIMIT 1",
                    (first.snapshot.version, end_text),
                ).fetchone()
                if uncertain is not None:
                    raise ScheduleVersionNotFound(
                        "A later schedule version has no effective boundary."
                    )
                rows = connection.execute(
                    "SELECT effective_at FROM schedule_effective_boundaries "
                    "WHERE effective_at > ? AND effective_at <= ? "
                    "ORDER BY effective_at LIMIT 257",
                    (start_text, end_text),
                ).fetchall()
                if len(rows) > 256:
                    raise ScheduleError("Discovery exceeds the schedule version limit.")
                versions = [first]
                versions.extend(
                    self._effective_at(connection, str(row[0])) for row in rows
                )
                candidates: list[tuple[datetime, str, EffectiveSchedule]] = []
                checks = 0
                for index, effective in enumerate(versions):
                    begin = max(start, effective.effective_at)
                    finish = (
                        versions[index + 1].effective_at - timedelta(microseconds=1)
                        if index + 1 < len(versions)
                        else end
                    )
                    if finish < begin:
                        continue
                    for schedule in effective.snapshot.schedules:
                        if not schedule.enabled:
                            continue
                        checks += 1
                        if checks > 10000:
                            raise ScheduleError(
                                "Discovery exceeds the schedule scan limit."
                            )
                        occurrences = (
                            interval_occurrences_between(
                                schedule, start=begin, end=finish, limit=limit
                            )
                            if isinstance(schedule, IntervalSchedule)
                            else daily_occurrences_between(
                                schedule, start=begin, end=finish
                            )
                        )
                        candidates.extend(
                            (instant, schedule.schedule_id, effective)
                            for instant in occurrences
                        )
                        if len(candidates) > limit:
                            raise ScheduleError(
                                "Discovery exceeds the occurrence limit."
                            )
                candidates.sort(key=lambda item: (item[0], item[1]))
                return tuple(
                    self._record_wake_decision(
                        connection, schedule_id, instant, now=now, effective=effective
                    )
                    for instant, schedule_id, effective in candidates
                )

    def _record_wake_decision(
        self,
        connection: sqlite3.Connection,
        schedule_id: str,
        scheduled_at: datetime,
        *,
        now: datetime | None = None,
        effective: EffectiveSchedule | None = None,
    ) -> WakeDecisionRecord:
        scheduled_text = _timestamp(scheduled_at)
        row = connection.execute(
            "SELECT schedule_id, scheduled_at, observed_at, schedule_version, decision "
            "FROM schedule_wake_decisions WHERE schedule_id = ? AND scheduled_at = ?",
            (schedule_id, scheduled_text),
        ).fetchone()
        if row is not None:
            return _decode_wake_decision(row)
        observed = _utc(self.clock.now()) if now is None else now
        if effective is None:
            effective = self._effective_at(connection, scheduled_text)
        schedule = next(
            (
                item
                for item in effective.snapshot.schedules
                if item.schedule_id == schedule_id
            ),
            None,
        )
        if schedule is None:
            raise ScheduleError("Schedule does not exist for this occurrence.")
        decision = decide_wake(schedule, scheduled_at=scheduled_at, woke_at=observed)
        connection.execute(
            "INSERT INTO schedule_wake_decisions VALUES (?, ?, ?, ?, ?)",
            (
                schedule_id,
                scheduled_text,
                _timestamp(observed),
                effective.snapshot.version,
                decision.value,
            ),
        )
        append_event(
            connection,
            occurred_at=observed,
            actor="system",
            action="schedule.wake.evaluated",
            object_type="schedule_occurrence",
            object_id=schedule_id,
            config_version=str(effective.snapshot.version),
            payload={"scheduled_at": scheduled_text, "decision": decision.value},
        )
        return WakeDecisionRecord(
            schedule_id,
            _utc(scheduled_at),
            observed,
            effective.snapshot.version,
            decision,
        )

    def _effective_at(
        self, connection: sqlite3.Connection, instant_text: str
    ) -> EffectiveSchedule:
        row = connection.execute(
            "SELECT b.version, b.effective_at, s.recorded_at "
            "FROM schedule_effective_boundaries AS b "
            "JOIN schedule_versions AS s ON s.version = b.version "
            "WHERE b.effective_at <= ? ORDER BY b.effective_at DESC LIMIT 1",
            (instant_text,),
        ).fetchone()
        if row is None:
            raise ScheduleVersionNotFound(
                f"No schedule has a recorded effective boundary at {instant_text}."
            )
        uncertain = connection.execute(
            "SELECT 1 FROM schedule_versions AS s "
            "LEFT JOIN schedule_effective_boundaries AS b ON b.version = s.version "
            "WHERE s.version > ? AND s.recorded_at <= ? AND b.version IS NULL LIMIT 1",
            (row[0], instant_text),
        ).fetchone()
        if uncertain is not None:
            raise ScheduleVersionNotFound(
                "A later schedule version has no effective boundary."
            )
        try:
            boundary = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
            recorded = datetime.fromisoformat(str(row[2]).replace("Z", "+00:00"))
            if (
                _timestamp(boundary) != row[1]
                or _timestamp(recorded) != row[2]
                or recorded > boundary
            ):
                raise ValueError("Invalid schedule boundary.")
        except (TypeError, ValueError) as exc:
            raise sqlite3.DatabaseError("Invalid schedule effective boundary.") from exc
        return EffectiveSchedule(self._load(connection, version=int(row[0])), boundary)

    @staticmethod
    def _load(
        connection: sqlite3.Connection, *, version: int | None = None
    ) -> ScheduleSnapshot:
        if version is None:
            row = connection.execute(
                "SELECT version, snapshot_json, snapshot_hash FROM schedule_versions "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT version, snapshot_json, snapshot_hash FROM schedule_versions "
                "WHERE version = ?",
                (version,),
            ).fetchone()
        if row is None:
            if version is not None:
                raise ScheduleVersionNotFound(f"Schedule version {version} not found.")
            return ScheduleSnapshot(0, ())
        payload = str(row[1])
        if sha256(payload.encode("utf-8")).hexdigest() != row[2]:
            raise sqlite3.DatabaseError("Schedule snapshot integrity check failed.")
        return ScheduleSnapshot(int(row[0]), _deserialize(payload))


def _decode_wake_decision(row: tuple[object, ...]) -> WakeDecisionRecord:
    try:
        schedule_id = str(row[0])
        scheduled = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(str(row[2]).replace("Z", "+00:00"))
        version = int(str(row[3]))
        decision = WakeDecision(str(row[4]))
        if (
            IDENTIFIER.fullmatch(schedule_id) is None
            or _timestamp(scheduled) != row[1]
            or _timestamp(observed) != row[2]
            or observed < scheduled
            or version < 1
        ):
            raise ValueError("Invalid wake decision.")
        return WakeDecisionRecord(schedule_id, scheduled, observed, version, decision)
    except (TypeError, ValueError) as exc:
        raise sqlite3.DatabaseError("Invalid stored schedule wake decision.") from exc
