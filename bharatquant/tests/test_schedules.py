import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra import database
from bq.infra.database import check_database, migrate
from bq.infra.schedules import (
    EffectiveSchedule,
    ScheduleConflict,
    ScheduleSnapshot,
    ScheduleStore,
    ScheduleVersionNotFound,
    WakeDecisionRecord,
    _serialize,
)
from bq.schedules import (
    DailySchedule,
    IntervalSchedule,
    ScheduleError,
    WakeDecision,
    WakePolicy,
    daily_occurrences_between,
    decide_wake,
    interval_occurrences_between,
    next_daily_occurrences,
    next_interval_occurrences,
    validate_schedules,
)


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 21, 15, 0, tzinfo=UTC)


class MovingClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def _schedule(
    schedule_id: str = "report", job_id: str = "synthetic-report"
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
        max_attempts=2,
    )


def _interval() -> IntervalSchedule:
    return IntervalSchedule(
        schedule_id="poll",
        job_id="synthetic-report",
        enabled=True,
        timezone="Asia/Kolkata",
        local_time=time(8),
        window_start=time(7, 45),
        window_end=time(10),
        wake_policy=WakePolicy.SKIP,
        wake_within_seconds=None,
        max_attempts=1,
        interval_minutes=60,
    )


def test_interval_occurrences_respect_local_window_and_wake_policy() -> None:
    schedule = _interval()
    start = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    expected = (start, start + timedelta(hours=1), start + timedelta(hours=2))
    assert (
        interval_occurrences_between(schedule, start=start, end=expected[-1])
        == expected
    )
    assert next_interval_occurrences(schedule, after=start, count=3) == (
        expected[1],
        expected[2],
        start + timedelta(days=1),
    )
    assert (
        decide_wake(schedule, scheduled_at=expected[1], woke_at=expected[1])
        is WakeDecision.RUN
    )
    assert (
        decide_wake(
            schedule,
            scheduled_at=expected[1],
            woke_at=expected[1] + timedelta(seconds=1),
        )
        is WakeDecision.SKIPPED_BY_POLICY
    )
    with pytest.raises(ScheduleError, match="interval"):
        decide_wake(
            schedule,
            scheduled_at=start + timedelta(minutes=1),
            woke_at=start + timedelta(hours=1),
        )
    with pytest.raises(ScheduleError, match="interval"):
        decide_wake(
            schedule,
            scheduled_at=start + timedelta(hours=3),
            woke_at=start + timedelta(hours=3),
        )
    for bad in (0, 1441, True):
        with pytest.raises(ScheduleError, match="Interval"):
            replace(schedule, interval_minutes=bad)
    with pytest.raises(ScheduleError, match="occurrence limit"):
        interval_occurrences_between(schedule, start=start, end=expected[-1], limit=2)
    with pytest.raises(ScheduleError, match="interval preview"):
        next_daily_occurrences(schedule, after=start)
    with pytest.raises(ScheduleError, match="interval discovery"):
        daily_occurrences_between(schedule, start=start, end=start)


def test_interval_discovery_enforces_limit_at_exact_boundary() -> None:
    schedule = replace(
        _interval(),
        timezone="UTC",
        local_time=time(0),
        window_start=time(0),
        window_end=time(23, 59),
        interval_minutes=1,
    )
    start = datetime(2026, 11, 21, tzinfo=UTC)
    assert (
        len(
            interval_occurrences_between(
                schedule, start=start, end=start + timedelta(minutes=999)
            )
        )
        == 1000
    )
    with pytest.raises(ScheduleError, match="occurrence limit"):
        interval_occurrences_between(
            schedule, start=start, end=start + timedelta(minutes=1000)
        )


def test_weekday_rules_apply_to_daily_and_interval_occurrences(tmp_path: Path) -> None:
    friday = datetime(2026, 11, 20, 2, 30, tzinfo=UTC)
    monday = friday + timedelta(days=3)
    weekdays = (0, 1, 2, 3, 4)
    daily = replace(_schedule(), weekdays=weekdays)
    interval = replace(_interval(), weekdays=weekdays)
    assert next_daily_occurrences(daily, after=friday, count=1) == (monday,)
    assert daily_occurrences_between(daily, start=friday, end=monday) == (
        friday,
        monday,
    )
    assert next_interval_occurrences(
        interval, after=friday + timedelta(hours=2), count=1
    ) == (monday,)
    assert interval_occurrences_between(
        interval, start=friday + timedelta(hours=2), end=monday
    ) == (friday + timedelta(hours=2), monday)
    saturday = friday + timedelta(days=1)
    with pytest.raises(ScheduleError, match="weekdays"):
        decide_wake(daily, scheduled_at=saturday, woke_at=saturday)
    with pytest.raises(ScheduleError, match="weekdays"):
        decide_wake(interval, scheduled_at=saturday, woke_at=saturday)
    for invalid in ((), (1, 0), (0, 0), (7,), (True,)):
        with pytest.raises(ScheduleError, match="weekdays"):
            replace(daily, weekdays=invalid)
    assert "weekdays" not in json.loads(_serialize((_schedule(),)))[0]
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = MovingClock(friday)
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    saved = store.save((daily, interval), expected_version=0)
    assert store.load() == saved
    clock.instant = monday
    decisions = store.record_due_daily(start=friday, end=monday)
    assert [
        item.scheduled_at for item in decisions if item.schedule_id == "report"
    ] == [friday, monday]
    assert [item.scheduled_at for item in decisions if item.schedule_id == "poll"] == [
        friday,
        friday + timedelta(hours=1),
        friday + timedelta(hours=2),
        monday,
    ]


def test_skipped_weekday_does_not_attempt_dst_resolution() -> None:
    schedule = replace(
        _interval(),
        timezone="America/New_York",
        local_time=time(1, 30),
        window_start=time(0),
        window_end=time(3, 30),
        weekdays=(0, 1, 2, 3, 4),
    )
    saturday = datetime(2026, 3, 7, 12, tzinfo=UTC)
    monday = datetime(2026, 3, 9, 5, 30, tzinfo=UTC)
    assert next_interval_occurrences(schedule, after=saturday, count=1) == (monday,)
    assert interval_occurrences_between(schedule, start=saturday, end=monday) == (
        monday,
    )


def test_daily_weekday_preview_fails_cleanly_at_date_limit() -> None:
    last_day = datetime(9999, 12, 31, 7, 59, tzinfo=UTC)
    schedule = replace(_schedule(), timezone="UTC", weekdays=(last_day.weekday(),))
    assert next_daily_occurrences(schedule, after=last_day, count=1) == (
        last_day + timedelta(minutes=1),
    )
    with pytest.raises(ScheduleError, match="outside the supported timezone"):
        next_daily_occurrences(schedule, after=last_day + timedelta(minutes=1), count=1)
    disabled_day = replace(schedule, weekdays=((last_day.weekday() + 1) % 7,))
    with pytest.raises(ScheduleError, match="outside the supported timezone"):
        next_daily_occurrences(disabled_day, after=last_day, count=1)


@pytest.mark.parametrize(
    ("day", "local_time", "after"),
    [
        (
            datetime(2026, 3, 8, 0, tzinfo=UTC),
            time(1, 30),
            datetime(2026, 3, 8, 6, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 1, 0, tzinfo=UTC),
            time(1, 30),
            datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
        ),
    ],
)
def test_interval_preview_and_discovery_fail_closed_on_dst_transition(
    day: datetime, local_time: time, after: datetime
) -> None:
    schedule = replace(
        _interval(),
        timezone="America/New_York",
        local_time=local_time,
        window_start=time(0),
        window_end=time(3, 30),
    )
    with pytest.raises(ScheduleError, match="ambiguous or nonexistent"):
        next_interval_occurrences(schedule, after=after)
    with pytest.raises(ScheduleError, match="ambiguous or nonexistent"):
        interval_occurrences_between(schedule, start=day, end=day + timedelta(hours=10))


def test_interval_schedule_versions_discovery_and_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    start = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(start - timedelta(hours=1))
    monkeypatch.setattr("bq.infra.schedules.SystemClock", lambda: clock)
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    interval = replace(_interval(), weekdays=(5,))
    first = store.save((interval,), expected_version=0)
    assert store.load() == first
    clock.instant = start + timedelta(minutes=90)
    second = store.save((replace(interval, interval_minutes=30),), expected_version=1)
    assert store.load() == second
    clock.instant = start + timedelta(hours=2)
    with pytest.raises(ScheduleError, match="occurrence limit"):
        store.record_due_daily(start=start, end=clock.now(), limit=3)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM schedule_wake_decisions"
        ).fetchone() == (0,)
    decisions = store.record_due_daily(start=start, end=clock.now())
    assert [item.scheduled_at for item in decisions] == [
        start,
        start + timedelta(hours=1),
        start + timedelta(minutes=90),
        start + timedelta(hours=2),
    ]
    assert [item.schedule_version for item in decisions] == [1, 1, 2, 2]
    assert [item.decision for item in decisions] == [
        WakeDecision.SKIPPED_BY_POLICY,
        WakeDecision.SKIPPED_BY_POLICY,
        WakeDecision.SKIPPED_BY_POLICY,
        WakeDecision.RUN,
    ]
    assert store.record_due_daily(start=start, end=clock.now()) == decisions
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_runs").fetchone() == (0,)
    check_database(path)
    auth = OwnerAuth(path)
    auth.setup("synthetic owner password")
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": "synthetic owner password"},
            )
            response = await client.get("/api/v1/schedules/status")
            assert response.status_code == 200
            body = response.json()
            status = body["schedules"][0]
            assert status["interval_minutes"] == 30
            assert status["local_time"] == "08:00"
            assert status["window_end"] == "10:00"
            assert status["weekdays"] == [5]
            assert status["preview_state"] == "available"
            assert status["next_occurrences"][0].startswith("2026-11-28T02:30:00")
            assert body["scheduler_running"] is False

    asyncio.run(check())


def test_daily_preview_is_utc_and_excludes_the_current_occurrence() -> None:
    schedule = _schedule()
    assert next_daily_occurrences(
        schedule, after=datetime(2026, 11, 21, 2, 30, tzinfo=UTC), count=2
    ) == (
        datetime(2026, 11, 22, 2, 30, tzinfo=UTC),
        datetime(2026, 11, 23, 2, 30, tzinfo=UTC),
    )
    with pytest.raises(ScheduleError, match="timezone-aware"):
        next_daily_occurrences(schedule, after=datetime(2026, 11, 21, 2, 30))
    with pytest.raises(ScheduleError, match="disabled"):
        next_daily_occurrences(
            replace(schedule, enabled=False), after=FrozenClock().now()
        )


@pytest.mark.parametrize(
    ("local_time", "after"),
    [
        (time(2, 30), datetime(2026, 3, 8, 0, 0, tzinfo=UTC)),
        (time(1, 30), datetime(2026, 11, 1, 0, 0, tzinfo=UTC)),
    ],
)
def test_preview_rejects_nonexistent_or_ambiguous_local_times(
    local_time: time, after: datetime
) -> None:
    schedule = replace(
        _schedule(),
        timezone="America/New_York",
        local_time=local_time,
        window_start=time(0),
        window_end=time(4),
    )
    with pytest.raises(ScheduleError, match="ambiguous or nonexistent"):
        next_daily_occurrences(schedule, after=after)

    assert (
        next_daily_occurrences(schedule, after=after.replace(hour=12), count=1)[
            0
        ].date()
        > after.date()
    )


def test_schedule_validates_operating_window_and_wake_policy() -> None:
    with pytest.raises(ScheduleError, match="operating window"):
        replace(_schedule(), local_time=time(6))
    with pytest.raises(ScheduleError, match="Wake window"):
        replace(_schedule(), wake_within_seconds=0)
    with pytest.raises(ScheduleError, match="only valid"):
        replace(_schedule(), wake_policy=WakePolicy.SKIP)
    with pytest.raises(ScheduleError, match="timezone"):
        replace(_schedule(), timezone="Not/A_Zone")


def test_wake_decision_respects_policy_cutoff_and_operating_window() -> None:
    schedule = _schedule()
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    assert decide_wake(schedule, scheduled_at=due, woke_at=due) is WakeDecision.RUN
    assert (
        decide_wake(schedule, scheduled_at=due, woke_at=due + timedelta(minutes=30))
        is WakeDecision.RUN
    )
    assert (
        decide_wake(
            schedule,
            scheduled_at=due,
            woke_at=due + timedelta(minutes=30, microseconds=1),
        )
        is WakeDecision.WAKE_WINDOW_EXPIRED
    )
    assert (
        decide_wake(
            replace(schedule, wake_within_seconds=10**100),
            scheduled_at=due,
            woke_at=due + timedelta(hours=1),
        )
        is WakeDecision.RUN
    )
    skip = replace(schedule, wake_policy=WakePolicy.SKIP, wake_within_seconds=None)
    assert decide_wake(skip, scheduled_at=due, woke_at=due) is WakeDecision.RUN
    assert (
        decide_wake(skip, scheduled_at=due, woke_at=due + timedelta(seconds=1))
        is WakeDecision.SKIPPED_BY_POLICY
    )
    always = replace(
        schedule, wake_policy=WakePolicy.ALWAYS_RUN, wake_within_seconds=None
    )
    assert (
        decide_wake(
            always, scheduled_at=due, woke_at=due + timedelta(hours=9, minutes=30)
        )
        is WakeDecision.RUN
    )
    assert (
        decide_wake(
            always,
            scheduled_at=due,
            woke_at=due + timedelta(hours=9, minutes=30, seconds=1),
        )
        is WakeDecision.OUTSIDE_OPERATING_WINDOW
    )
    assert (
        decide_wake(always, scheduled_at=due, woke_at=due + timedelta(days=1))
        is WakeDecision.RUN
    )


def test_wake_decision_rejects_invalid_or_ambiguous_occurrences() -> None:
    schedule = _schedule()
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    with pytest.raises(ScheduleError, match="timezone-aware"):
        decide_wake(schedule, scheduled_at=due.replace(tzinfo=None), woke_at=due)
    with pytest.raises(ScheduleError, match="before"):
        decide_wake(schedule, scheduled_at=due, woke_at=due - timedelta(seconds=1))
    with pytest.raises(ScheduleError, match="scheduled local time"):
        decide_wake(
            schedule,
            scheduled_at=due + timedelta(minutes=1),
            woke_at=due + timedelta(hours=1),
        )
    with pytest.raises(ScheduleError, match="disabled"):
        decide_wake(replace(schedule, enabled=False), scheduled_at=due, woke_at=due)
    ambiguous = replace(
        schedule,
        timezone="America/New_York",
        local_time=time(1, 30),
        window_start=time(0),
        window_end=time(4),
    )
    first_fold = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    with pytest.raises(ScheduleError, match="ambiguous or nonexistent"):
        decide_wake(ambiguous, scheduled_at=first_fold, woke_at=first_fold)


def test_schedule_dependencies_require_enabled_implemented_jobs_and_no_cycles() -> None:
    first = _schedule("first", "job-a")
    second = replace(_schedule("second", "job-b"), dependencies=("first",))
    available = frozenset({"job-a", "job-b"})
    validate_schedules((first, second), available_jobs=available)
    with pytest.raises(ScheduleError, match="not implemented"):
        validate_schedules((first, second), available_jobs=frozenset({"job-a"}))
    with pytest.raises(ScheduleError, match="Missing"):
        validate_schedules((second,), available_jobs=available)
    with pytest.raises(ScheduleError, match="disabled"):
        validate_schedules(
            (replace(first, enabled=False), second), available_jobs=available
        )
    with pytest.raises(ScheduleError, match="cycle"):
        validate_schedules(
            (replace(first, dependencies=("second",)), second),
            available_jobs=available,
        )
    with pytest.raises(ScheduleError, match="unique"):
        validate_schedules((first, first), available_jobs=available)


def test_schedule_snapshots_are_versioned_audited_and_fail_on_stale_saves(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=FrozenClock()
    )
    schedule = _schedule()
    assert store.load() == ScheduleSnapshot(0, ())
    first = store.save((schedule,), expected_version=0)
    assert first == ScheduleSnapshot(1, (schedule,))
    assert store.load() == first
    with pytest.raises(ScheduleConflict, match="reload"):
        store.save((), expected_version=0)
    second = store.save(
        (replace(schedule, enabled=False),),
        expected_version=1,
        effective_at=FrozenClock().now() + timedelta(microseconds=1),
    )
    assert second.version == 2
    assert not store.load().schedules[0].enabled
    assert store.load(1) == first
    assert store.effective_at(FrozenClock().now()) == EffectiveSchedule(
        first, FrozenClock().now()
    )
    assert store.effective_at(FrozenClock().now() + timedelta(microseconds=1)) == (
        EffectiveSchedule(second, FrozenClock().now() + timedelta(microseconds=1))
    )
    with pytest.raises(ScheduleVersionNotFound):
        store.load(3)

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT action FROM audit_events ORDER BY sequence"
        ).fetchall() == [
            ("schedule.snapshot.saved",),
            ("schedule.snapshot.saved",),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM schedule_versions WHERE version = 1")
    check_database(path)


def test_schedule_store_rejects_unimplemented_jobs_and_corrupt_snapshots(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ScheduleStore(path, available_jobs=frozenset(), clock=FrozenClock())
    with pytest.raises(ScheduleError, match="not implemented"):
        store.save((_schedule(),), expected_version=0)
    store.save((replace(_schedule(), enabled=False),), expected_version=0)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER schedule_versions_no_update")
        connection.execute("UPDATE schedule_versions SET snapshot_json = '[]'")
    with pytest.raises(sqlite3.DatabaseError, match="integrity check failed"):
        store.load()


def test_concurrent_schedule_saves_cannot_overwrite_each_other(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=FrozenClock()
    )
    barrier = Barrier(2)

    def save(schedule: DailySchedule) -> str:
        barrier.wait()
        try:
            store.save((schedule,), expected_version=0)
        except ScheduleConflict:
            return "conflict"
        return "saved"

    schedules = (_schedule("first"), _schedule("second"))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, schedules))
    assert sorted(results) == ["conflict", "saved"]
    assert store.load().version == 1
    check_database(path)


def test_schedule_save_rolls_back_if_audit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=FrozenClock()
    )

    def fail_audit(*args: object, **kwargs: object) -> None:
        raise sqlite3.DatabaseError("Synthetic audit failure")

    monkeypatch.setattr("bq.infra.schedules.append_event", fail_audit)
    with pytest.raises(sqlite3.DatabaseError, match="Synthetic audit failure"):
        store.save((_schedule(),), expected_version=0)
    assert store.load() == ScheduleSnapshot(0, ())
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_effective_boundaries"
        ).fetchone() == (0,)
    check_database(path)


def test_data_import_migration_preserves_existing_schedule_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrations = database.packaged_migrations()
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations[:7])
    assert migrate(path, create=True) == 7
    schedule = _schedule()
    payload = _serialize((schedule,))
    timestamp = (
        FrozenClock().now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO schedule_versions VALUES (?, ?, ?, ?)",
            (1, timestamp, payload, sha256(payload.encode()).hexdigest()),
        )
    snapshot = ScheduleSnapshot(1, (schedule,))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=FrozenClock()
    )
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations)
    assert migrate(path) == 6
    assert store.load() == snapshot
    with pytest.raises(ScheduleVersionNotFound, match="effective boundary"):
        store.effective_at(FrozenClock().now())
    replacement = store.save((schedule,), expected_version=1)
    assert store.effective_at(FrozenClock().now()) == EffectiveSchedule(
        replacement, FrozenClock().now()
    )
    check_database(path)


def test_effective_boundaries_reject_backdating_and_nonadvancing_versions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    now = FrozenClock().now()
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=FrozenClock()
    )
    with pytest.raises(ScheduleError, match="timezone-aware"):
        store.save(
            (_schedule(),), expected_version=0, effective_at=now.replace(tzinfo=None)
        )
    with pytest.raises(ScheduleError, match="precede"):
        store.save(
            (_schedule(),),
            expected_version=0,
            effective_at=now - timedelta(microseconds=1),
        )
    first = store.save(
        (_schedule(),), expected_version=0, effective_at=now + timedelta(seconds=10)
    )
    with pytest.raises(ScheduleVersionNotFound):
        store.effective_at(now + timedelta(seconds=9))
    assert store.effective_at(now + timedelta(seconds=10)) == EffectiveSchedule(
        first, now + timedelta(seconds=10)
    )
    with pytest.raises(ScheduleError, match="must advance"):
        store.save(
            (_schedule(),), expected_version=1, effective_at=now + timedelta(seconds=10)
        )
    with pytest.raises(ScheduleError, match="must advance"):
        store.save((_schedule(),), expected_version=1)
    earlier_store = ScheduleStore(
        path,
        available_jobs=frozenset({"synthetic-report"}),
        clock=MovingClock(now - timedelta(seconds=1)),
    )
    with pytest.raises(ScheduleError, match="clock cannot move backwards"):
        earlier_store.save(
            (_schedule(),), expected_version=1, effective_at=now + timedelta(seconds=20)
        )
    assert store.load() == first
    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM schedule_effective_boundaries")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE schedule_effective_boundaries SET effective_at = 'changed'"
            )


def test_effective_lookup_fails_closed_for_later_legacy_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=FrozenClock()
    )
    first = store.save((_schedule(),), expected_version=0)
    legacy_time = FrozenClock().now() + timedelta(seconds=1)
    payload = _serialize((replace(_schedule(), enabled=False),))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO schedule_versions VALUES (?, ?, ?, ?)",
            (
                2,
                legacy_time.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                payload,
                sha256(payload.encode()).hexdigest(),
            ),
        )
    assert store.effective_at(FrozenClock().now()).snapshot == first
    with pytest.raises(ScheduleVersionNotFound, match="later schedule"):
        store.effective_at(legacy_time)


def test_recorded_wake_decisions_use_occurrence_version_and_are_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due - timedelta(hours=1))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save((_schedule(),), expected_version=0)
    clock.instant = due + timedelta(minutes=5)
    skip = replace(_schedule(), wake_policy=WakePolicy.SKIP, wake_within_seconds=None)
    store.save((skip,), expected_version=1)
    clock.instant = due + timedelta(minutes=10)
    first = store.record_wake_decision("report", scheduled_at=due)
    assert first.schedule_version == 1
    assert first.decision is WakeDecision.RUN
    assert first.observed_at == clock.now()
    clock.instant = due + timedelta(hours=1)
    assert store.record_wake_decision("report", scheduled_at=due) == first
    tomorrow = due + timedelta(days=1)
    with pytest.raises(ScheduleError, match="before"):
        store.record_wake_decision("report", scheduled_at=tomorrow)
    with pytest.raises(ScheduleError, match="does not exist"):
        store.record_wake_decision("missing", scheduled_at=due)
    clock.instant = tomorrow + timedelta(minutes=1)
    barrier = Barrier(2)

    def record() -> WakeDecisionRecord:
        barrier.wait()
        return store.record_wake_decision("report", scheduled_at=tomorrow)

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(executor.map(lambda _: record(), range(2)))
    assert decisions[0] == decisions[1]
    assert decisions[0].schedule_version == 2
    assert decisions[0].decision is WakeDecision.SKIPPED_BY_POLICY
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_wake_decisions"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'schedule.wake.evaluated'"
        ).fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM job_runs").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM schedule_wake_decisions")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE schedule_wake_decisions SET decision = 'RUN'")
    check_database(path)


def test_missed_decision_is_recorded_and_audit_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due - timedelta(hours=1))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save((_schedule(),), expected_version=0)
    clock.instant = due + timedelta(minutes=30, microseconds=1)

    def fail_audit(*args: object, **kwargs: object) -> None:
        raise sqlite3.DatabaseError("Synthetic audit failure")

    with monkeypatch.context() as patch:
        patch.setattr("bq.infra.schedules.append_event", fail_audit)
        with pytest.raises(sqlite3.DatabaseError, match="Synthetic audit failure"):
            store.record_wake_decision("report", scheduled_at=due)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_wake_decisions"
        ).fetchone() == (0,)
    decision = store.record_wake_decision("report", scheduled_at=due)
    assert decision.decision is WakeDecision.WAKE_WINDOW_EXPIRED
    clock.instant += timedelta(hours=1)
    assert store.record_wake_decision("report", scheduled_at=due) == decision


def test_wake_decision_migration_preserves_effective_schedules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bq.db"
    migrations = database.packaged_migrations()
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations[:12])
    assert migrate(path, create=True) == 12
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=FrozenClock()
    )
    snapshot = store.save((_schedule(),), expected_version=0)
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations)
    assert migrate(path) == 1
    assert store.effective_at(FrozenClock().now()).snapshot == snapshot
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_wake_decisions"
        ).fetchone() == (0,)
    check_database(path)


def test_wake_decision_uses_clock_after_acquiring_the_database_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due - timedelta(hours=1))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save((_schedule(),), expected_version=0)
    clock.instant = due + timedelta(minutes=30)
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
                store.record_wake_decision, "report", scheduled_at=due
            )
            try:
                assert entered.wait(timeout=5)
                clock.instant += timedelta(microseconds=1)
            finally:
                blocker.commit()
            decision = future.result(timeout=5)
    assert decision.observed_at == clock.now()
    assert decision.decision is WakeDecision.WAKE_WINDOW_EXPIRED


def test_daily_discovery_respects_version_boundaries_and_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due - timedelta(hours=1))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save((_schedule(),), expected_version=0)
    clock.instant = due
    delayed = replace(_schedule(), local_time=time(8, 10))
    store.save((delayed,), expected_version=1)
    clock.instant = due + timedelta(minutes=1)
    store.save(
        (_schedule(),),
        expected_version=2,
        effective_at=due + timedelta(days=1, minutes=20),
    )
    start = due - timedelta(minutes=5)
    end = due + timedelta(days=2, hours=1)
    clock.instant = end
    with pytest.raises(ScheduleError, match="occurrence limit"):
        store.record_due_daily(start=start, end=end, limit=2)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_wake_decisions"
        ).fetchone() == (0,)
    found = store.record_due_daily(start=start, end=end)
    assert [record.scheduled_at for record in found] == [
        due + timedelta(minutes=10),
        due + timedelta(days=1, minutes=10),
        due + timedelta(days=2),
    ]
    assert [record.schedule_version for record in found] == [2, 2, 3]
    assert all(record.decision is WakeDecision.WAKE_WINDOW_EXPIRED for record in found)
    clock.instant += timedelta(hours=1)
    assert store.record_due_daily(start=start, end=end) == found
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM job_runs").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM schedule_wake_decisions"
        ).fetchone() == (3,)
    check_database(path)


def test_daily_discovery_rejects_ambiguous_time_and_invalid_bounds(
    tmp_path: Path,
) -> None:
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    assert daily_occurrences_between(_schedule(), start=due, end=due) == (due,)
    assert (
        len(
            daily_occurrences_between(
                _schedule(), start=due, end=due + timedelta(days=31)
            )
        )
        == 32
    )
    with pytest.raises(ScheduleError, match="31 days"):
        daily_occurrences_between(
            _schedule(), start=due, end=due + timedelta(days=31, microseconds=1)
        )
    with pytest.raises(ScheduleError, match="timezone-aware"):
        daily_occurrences_between(_schedule(), start=due.replace(tzinfo=None), end=due)
    ambiguous = replace(
        _schedule(),
        timezone="America/New_York",
        local_time=time(1, 30),
        window_start=time(0),
        window_end=time(4),
    )
    start = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    end = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    with pytest.raises(ScheduleError, match="ambiguous or nonexistent"):
        daily_occurrences_between(ambiguous, start=start, end=end)
    assert (
        daily_occurrences_between(
            ambiguous, start=datetime(2026, 11, 1, 7, 0, tzinfo=UTC), end=end
        )
        == ()
    )
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = MovingClock(start - timedelta(hours=1))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save((ambiguous,), expected_version=0)
    clock.instant = end
    with pytest.raises(ScheduleError, match="ambiguous or nonexistent"):
        store.record_due_daily(start=start, end=end)
    with pytest.raises(ScheduleError, match="future"):
        store.record_due_daily(start=start, end=end + timedelta(seconds=1))
    for limit in (0, 1001, True):
        with pytest.raises(ScheduleError, match="limit"):
            store.record_due_daily(start=start, end=end, limit=limit)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_wake_decisions"
        ).fetchone() == (0,)


def test_daily_discovery_rolls_back_batch_on_audit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bq.infra.audit import append_event

    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due - timedelta(hours=1))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save((_schedule(),), expected_version=0)
    clock.instant = due + timedelta(days=1, minutes=1)
    calls = 0

    def fail_second(connection: sqlite3.Connection, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.DatabaseError("Synthetic audit failure")
        append_event(connection, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("bq.infra.schedules.append_event", fail_second)
    with pytest.raises(sqlite3.DatabaseError, match="Synthetic audit failure"):
        store.record_due_daily(start=due, end=clock.now())
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_wake_decisions"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'schedule.wake.evaluated'"
        ).fetchone() == (0,)


def test_daily_discovery_fails_closed_for_an_unbounded_version_in_range(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due - timedelta(hours=1))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save((_schedule(),), expected_version=0)
    payload = _serialize((_schedule(),))
    legacy_time = due + timedelta(hours=1)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO schedule_versions VALUES (?, ?, ?, ?)",
            (
                2,
                legacy_time.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                payload,
                sha256(payload.encode()).hexdigest(),
            ),
        )
    clock.instant = due + timedelta(days=1)
    with pytest.raises(ScheduleVersionNotFound, match="later schedule"):
        store.record_due_daily(start=due, end=clock.now())
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_wake_decisions"
        ).fetchone() == (0,)


@pytest.mark.parametrize("count", [1000, 1001])
def test_daily_discovery_enforces_exact_occurrence_cap(
    tmp_path: Path,
    count: int,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due - timedelta(hours=1))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save(
        tuple(
            replace(_schedule(), schedule_id=f"job-{number}") for number in range(count)
        ),
        expected_version=0,
    )
    clock.instant = due
    if count == 1000:
        assert len(store.record_due_daily(start=due, end=due, limit=1000)) == 1000
    else:
        with pytest.raises(ScheduleError, match="occurrence limit"):
            store.record_due_daily(start=due, end=due, limit=1000)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_wake_decisions"
        ).fetchone() == (1000 if count == 1000 else 0,)


def test_daily_discovery_enforces_exact_version_cap(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    start = datetime(2026, 11, 21, 2, 0, tzinfo=UTC)
    clock = MovingClock(start)
    store = ScheduleStore(path, available_jobs=frozenset(), clock=clock)
    store.save((), expected_version=0)
    payload = _serialize(())
    digest = sha256(payload.encode()).hexdigest()
    versions = [
        (
            index + 1,
            (start + timedelta(microseconds=index))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        )
        for index in range(1, 258)
    ]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO schedule_versions VALUES (?, ?, ?, ?)",
            [(version, recorded, payload, digest) for version, recorded in versions],
        )
        connection.executemany(
            "INSERT INTO schedule_effective_boundaries VALUES (?, ?)", versions
        )
    clock.instant = start + timedelta(seconds=1)
    assert (
        store.record_due_daily(start=start, end=start + timedelta(microseconds=256))
        == ()
    )
    with pytest.raises(ScheduleError, match="version limit"):
        store.record_due_daily(start=start, end=start + timedelta(microseconds=257))


def test_daily_discovery_uses_clock_after_acquiring_the_database_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due - timedelta(hours=1))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save((_schedule(),), expected_version=0)
    clock.instant = due + timedelta(minutes=30)
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
            future = executor.submit(store.record_due_daily, start=due, end=due)
            try:
                assert entered.wait(timeout=5)
                clock.instant += timedelta(microseconds=1)
            finally:
                blocker.commit()
            decisions = future.result(timeout=5)
    assert decisions[0].observed_at == clock.now()
    assert decisions[0].decision is WakeDecision.WAKE_WINDOW_EXPIRED


def test_concurrent_daily_discovery_records_each_occurrence_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due - timedelta(hours=1))
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save((_schedule(),), expected_version=0)
    clock.instant = due + timedelta(days=1, minutes=1)
    barrier = Barrier(2)

    def discover() -> tuple[WakeDecisionRecord, ...]:
        barrier.wait()
        return store.record_due_daily(start=due, end=clock.now())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: discover(), range(2)))
    assert results[0] == results[1]
    assert len(results[0]) == 2
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM schedule_wake_decisions"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'schedule.wake.evaluated'"
        ).fetchone() == (2,)


def test_schedule_status_is_private_bounded_and_distinguishes_pending_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due - timedelta(hours=1))
    monkeypatch.setattr("bq.infra.schedules.SystemClock", lambda: clock)
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    auth = OwnerAuth(path)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.get("/api/v1/schedules/status")
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            auth.setup("synthetic owner password")
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": "synthetic owner password"},
            )
            csrf = login.json()["csrf_token"]
            empty = (await client.get("/api/v1/schedules/status")).json()
            assert empty["latest_version"] == 0
            assert empty["effective_state"] == "not_configured"
            assert empty["scheduler_running"] is False
            schedules = tuple(
                replace(_schedule(), schedule_id=f"job-{index:02}")
                for index in range(21)
            )
            store.save(schedules, expected_version=0, effective_at=due)
            pending = (await client.get("/api/v1/schedules/status")).json()
            assert pending["latest_version"] == 1
            assert pending["effective_state"] == "unknown"
            assert pending["effective_version"] is None
            clock.instant = due
            store.record_due_daily(start=due, end=due)
            clock.instant += timedelta(seconds=1)
            preview = (await client.get("/api/v1/schedules/status")).json()
            assert preview["schedules"][0]["preview_state"] == "available"
            assert [
                datetime.fromisoformat(value)
                for value in preview["schedules"][0]["next_occurrences"]
            ] == [due + timedelta(days=index) for index in range(1, 6)]
            store.save(
                schedules, expected_version=1, effective_at=due + timedelta(days=1)
            )
            response = await client.get("/api/v1/schedules/status")
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["latest_version"] == 2
            assert body["effective_state"] == "available"
            assert body["effective_version"] == 1
            assert body["schedule_count"] == 21
            assert len(body["schedules"]) == 20
            assert body["schedules"][0]["schedule_id"] == "job-00"
            assert body["schedules"][0]["preview_state"] == "pending_change"
            assert body["schedules"][0]["next_occurrences"] == []
            assert body["decision_count"] == 21
            assert len(body["recent_decisions"]) == 20
            assert body["recent_decisions"][0]["decision"] == "RUN"
            assert body["scheduler_running"] is False
            assert (
                await client.post(
                    "/api/v1/auth/logout",
                    headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                )
            ).status_code == 204
            assert (await client.get("/api/v1/schedules/status")).status_code == 401

    asyncio.run(check())


def test_schedule_status_preview_reports_disabled_and_nonexistent_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = MovingClock(datetime(2026, 3, 7, 12, 0, tzinfo=UTC))
    monkeypatch.setattr("bq.infra.schedules.SystemClock", lambda: clock)
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    store.save(
        (
            replace(_schedule(), schedule_id="disabled", enabled=False),
            replace(
                _schedule(),
                schedule_id="gap",
                timezone="America/New_York",
                local_time=time(2, 30),
                window_start=time(0),
                window_end=time(4),
            ),
        ),
        expected_version=0,
    )
    auth = OwnerAuth(path)
    auth.setup("synthetic owner password")
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": "synthetic owner password"},
            )
            response = await client.get("/api/v1/schedules/status")
            assert response.status_code == 200
            schedules = response.json()["schedules"]
            assert [
                (item["schedule_id"], item["preview_state"]) for item in schedules
            ] == [
                ("disabled", "disabled"),
                ("gap", "unavailable"),
            ]
            assert all(item["next_occurrences"] == [] for item in schedules)

    asyncio.run(check())


def test_schedule_overview_reads_one_database_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    due = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)
    clock = MovingClock(due)
    store = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    writer = ScheduleStore(
        path, available_jobs=frozenset({"synthetic-report"}), clock=clock
    )
    first = store.save((_schedule(),), expected_version=0)
    original_load = store._load
    written = False

    def write_between_reads(
        connection: sqlite3.Connection, *, version: int | None = None
    ) -> ScheduleSnapshot:
        nonlocal written
        snapshot = original_load(connection, version=version)
        if not written:
            written = True
            writer.record_wake_decision("report", scheduled_at=due)
            clock.instant += timedelta(seconds=1)
            writer.save((), expected_version=1)
        return snapshot

    monkeypatch.setattr(store, "_load", write_between_reads)
    overview = store.overview()
    assert overview.latest == first
    assert overview.effective is not None
    assert overview.effective.snapshot == first
    assert overview.decision_count == 0
    assert overview.recent_decisions == ()

    updated = store.overview()
    assert updated.latest.version == 2
    assert updated.effective is not None
    assert updated.effective.snapshot.version == 2
    assert updated.decision_count == 1
    assert len(updated.recent_decisions) == 1
