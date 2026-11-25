import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.cli import main
from bq.data.calendar import (
    CalendarSession,
    CalendarSessionVersion,
    ExchangeCalendar,
    SessionClass,
    SessionState,
    UncoveredCalendarDate,
)
from bq.infra.database import check_database, migrate


class FrozenClock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


def _session() -> CalendarSession:
    return CalendarSession(
        exchange_code="TEST",
        session_date=date(2026, 11, 23),
        session_class=SessionClass.REGULAR,
        state=SessionState.OPEN,
        opens_at=datetime(2026, 11, 23, 3, 0, tzinfo=UTC),
        closes_at=datetime(2026, 11, 23, 10, 0, tzinfo=UTC),
        source_url="https://example.test/fixture-calendar",
        source_published_at=datetime(2026, 11, 20, 10, 0, tzinfo=UTC),
        known_at=datetime(2026, 11, 21, 12, 0, tzinfo=UTC),
        reviewed_by="owner",
        reviewed_at=datetime(2026, 11, 21, 13, 0, tzinfo=UTC),
    )


def test_calendar_is_unseeded_and_uses_only_reviewed_as_of_revisions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    calendar = ExchangeCalendar(path, clock)
    trading_date = date(2026, 11, 23)

    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM calendar_sessions").fetchone()[0]
            == 0
        )
    with pytest.raises(UncoveredCalendarDate):
        calendar.session_for(
            "TEST",
            trading_date,
            as_of=datetime(2026, 11, 21, 14, 0, tzinfo=UTC),
        )

    first = _session()
    clock.instant = first.reviewed_at + timedelta(minutes=30)
    first_id = calendar.record(first)
    assert (
        calendar.session_for(
            "TEST", trading_date, as_of=datetime(2026, 11, 21, 14, 0, tzinfo=UTC)
        )
        == first
    )

    second_known = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)
    second_reviewed = second_known + timedelta(hours=1)
    second = replace(
        first,
        session_class=SessionClass.SPECIAL,
        known_at=second_known,
        reviewed_at=second_reviewed,
        closes_at=datetime(2026, 11, 23, 11, 0, tzinfo=UTC),
    )
    clock.instant = second_reviewed + timedelta(minutes=1)
    second_id = calendar.record(second)
    assert first_id != second_id
    assert (
        calendar.session_for(
            "TEST", trading_date, as_of=second_known + timedelta(minutes=30)
        )
        == first
    )
    assert (
        calendar.session_for(
            "TEST", trading_date, as_of=second_reviewed + timedelta(minutes=1)
        )
        == second
    )

    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE calendar_sessions SET state = 'CLOSED' WHERE session_id = ?",
                (first_id,),
            )
    check_database(path)


def test_calendar_rejects_invalid_or_unreviewed_session_data() -> None:
    session = _session()

    with pytest.raises(ValueError, match="both time boundaries"):
        replace(session, closes_at=None)
    with pytest.raises(ValueError, match="close must follow"):
        replace(session, closes_at=session.opens_at)
    with pytest.raises(ValueError, match="reviewed by the owner"):
        replace(session, reviewed_by="unverified")
    with pytest.raises(ValueError, match="publication"):
        replace(
            session,
            source_published_at=datetime(2026, 11, 22, 10, 0, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        ExchangeCalendar(Path("unused")).session_for(
            "TEST", session.session_date, as_of=datetime(2026, 11, 21, 14, 0)
        )


def test_calendar_does_not_backdate_late_recorded_reviews(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    calendar = ExchangeCalendar(path, clock)
    session = _session()
    calendar.record(session)

    with pytest.raises(UncoveredCalendarDate):
        calendar.session_for(
            "TEST", session.session_date, as_of=clock.now() - timedelta(seconds=1)
        )
    assert (
        calendar.session_for("TEST", session.session_date, as_of=clock.now()) == session
    )


def test_calendar_rejects_revisions_when_the_record_clock_does_not_advance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    calendar = ExchangeCalendar(path, clock)
    first = _session()
    calendar.record(first)
    revision = replace(
        first,
        session_class=SessionClass.SPECIAL,
        known_at=first.known_at + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="time must advance"):
        calendar.record(revision)
    assert calendar.session_for("TEST", first.session_date, as_of=clock.now()) == first
    clock.instant += timedelta(microseconds=1)
    calendar.record(revision)
    assert (
        calendar.session_for("TEST", first.session_date, as_of=clock.now()) == revision
    )


def test_calendar_coverage_is_bounded_and_exposes_only_recorded_versions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    calendar = ExchangeCalendar(path, clock)
    session = _session()
    version_id = calendar.record(session)
    start = session.session_date - timedelta(days=1)
    end = session.session_date + timedelta(days=1)

    before = calendar.coverage(
        "TEST", start=start, end=end, as_of=clock.now() - timedelta(microseconds=1)
    )
    assert all(day.version is None for day in before)
    coverage = calendar.coverage("TEST", start=start, end=end, as_of=clock.now())
    assert [day.session_date for day in coverage] == [start, session.session_date, end]
    assert coverage[0].version is None and coverage[2].version is None
    version = coverage[1].version
    assert version is not None
    assert (version.session_id, version.recorded_at, version.session) == (
        version_id,
        clock.now(),
        session,
    )
    assert (
        len(
            calendar.coverage(
                "TEST", start=start, end=start + timedelta(days=30), as_of=clock.now()
            )
        )
        == 31
    )
    with pytest.raises(ValueError, match="1 to 31"):
        calendar.coverage(
            "TEST", start=start, end=start + timedelta(days=31), as_of=clock.now()
        )
    with pytest.raises(ValueError, match="1 to 31"):
        calendar.coverage("TEST", start=end, end=start, as_of=clock.now())


def test_calendar_coverage_uses_one_snapshot_during_concurrent_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    calendar = ExchangeCalendar(path, clock)
    writer = ExchangeCalendar(path, clock)
    first = _session()
    calendar.record(first)
    next_date = first.session_date + timedelta(days=1)
    second = replace(
        first,
        session_date=next_date,
        opens_at=first.opens_at + timedelta(days=1) if first.opens_at else None,
        closes_at=first.closes_at + timedelta(days=1) if first.closes_at else None,
    )
    original = calendar._session_version
    written = False

    def write_between_reads(
        connection: sqlite3.Connection, exchange: str, day: date, as_of: str
    ) -> CalendarSessionVersion | None:
        nonlocal written
        version = original(connection, exchange, day, as_of)
        if not written:
            written = True
            writer.record(second)
        return version

    monkeypatch.setattr(calendar, "_session_version", write_between_reads)
    coverage = calendar.coverage(
        "TEST", start=first.session_date, end=next_date, as_of=clock.now()
    )
    assert coverage[0].version is not None
    assert coverage[1].version is None
    assert (
        calendar.coverage("TEST", start=next_date, end=next_date, as_of=clock.now())[
            0
        ].version
        is not None
    )


def test_calendar_coverage_api_requires_auth_and_never_infers_uncovered_dates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    session = _session()
    calendar = ExchangeCalendar(path, clock)
    session_id = calendar.record(session)
    closed_id = calendar.record(
        replace(
            session,
            session_date=session.session_date + timedelta(days=1),
            session_class=SessionClass.HOLIDAY,
            state=SessionState.CLOSED,
            opens_at=None,
            closes_at=None,
        )
    )
    OwnerAuth(path).setup("synthetic owner password")
    app = create_app(database_path=path)
    params = {
        "exchange_code": "TEST",
        "start": "2026-11-22",
        "end": "2026-11-24",
        "as_of": clock.now().isoformat(),
    }

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.get("/api/v1/calendar/coverage", params=params)
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": "synthetic owner password"},
            )
            assert login.status_code == 200
            response = await client.get("/api/v1/calendar/coverage", params=params)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            days = response.json()["days"]
            assert [day["state"] for day in days] == ["UNCOVERED", "OPEN", "CLOSED"]
            assert days[1]["session_id"] == session_id
            assert days[1]["session_class"] == "REGULAR"
            assert "source_url" not in days[1]
            assert days[0]["session_id"] is None
            assert days[2]["session_id"] == closed_id
            assert days[2]["session_class"] == "HOLIDAY"
            assert days[2]["opens_at"] is None
            historical = params | {
                "as_of": (clock.now() - timedelta(microseconds=1)).isoformat()
            }
            assert all(
                day["state"] == "UNCOVERED"
                for day in (
                    await client.get("/api/v1/calendar/coverage", params=historical)
                ).json()["days"]
            )
            for bad in (
                params | {"as_of": "2026-11-21T15:00:00"},
                params | {"as_of": "0001-01-01T00:00:00+23:00"},
                params | {"end": "2026-12-23"},
                params | {"start": "20261122"},
                params | {"exchange_code": "invalid"},
            ):
                assert (
                    await client.get("/api/v1/calendar/coverage", params=bad)
                ).status_code == 422
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TRIGGER calendar_sessions_no_update")
                connection.execute(
                    "UPDATE calendar_sessions SET source_url = ? WHERE session_id = ?",
                    ("https://user:pass@example.test/data", session_id),
                )
            response = await client.get("/api/v1/calendar/coverage", params=params)
            assert response.status_code == 500

    asyncio.run(check())


def test_calendar_cli_requires_review_and_records_current_knowledge_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    clock = FrozenClock()
    monkeypatch.setattr("bq.cli.SystemClock", lambda: clock)
    args = [
        "calendar",
        "record-session",
        "--exchange-code",
        "TEST",
        "--date",
        "2026-11-23",
        "--class",
        "REGULAR",
        "--state",
        "OPEN",
        "--opens-at",
        "2026-11-23T03:00:00Z",
        "--closes-at",
        "2026-11-23T10:00:00Z",
        "--source-url",
        "https://example.test/calendar",
    ]
    with pytest.raises(SystemExit):
        main(args)
    capsys.readouterr()
    invalid = args + ["--confirm-review"]
    invalid[invalid.index("--source-url") + 1] = (
        "https://example.test/calendar?secret=synthetic"
    )
    assert main(invalid) == 1
    assert "without credentials, query or fragment" in capsys.readouterr().err
    assert main(args + ["--confirm-review"]) == 0
    assert "not independently verified" in capsys.readouterr().out
    calendar = ExchangeCalendar(path, clock)
    with pytest.raises(UncoveredCalendarDate):
        calendar.session_for(
            "TEST", date(2026, 11, 23), as_of=clock.now() - timedelta(microseconds=1)
        )
    saved = calendar.session_for("TEST", date(2026, 11, 23), as_of=clock.now())
    assert saved.known_at == saved.reviewed_at == clock.now()
    assert saved.state is SessionState.OPEN
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'calendar.session.recorded'"
        ).fetchone() == (1,)
