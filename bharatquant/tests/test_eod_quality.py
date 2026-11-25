import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from bq.cli import main
from bq.data.calendar import (
    CalendarSession,
    ExchangeCalendar,
    SessionClass,
    SessionState,
    UncoveredCalendarDate,
)
from bq.data.csv_eod import Adjustment, import_eod_csv
from bq.data.eod_quality import (
    CalendarEodQualityReport,
    EodQualityReport,
    inspect_eod_import_with_calendar,
    inspect_eod_quality,
)
from bq.data.records import DataRecordStore, MissingDataRecord, QualityStatus
from bq.infra.database import migrate

HEADER = b"instrument_id,trading_date,event_at,open,high,low,close,volume\n"
DATES = tuple(date(2026, 11, day) for day in range(17, 22))


class Clock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


def import_rows(
    store: DataRecordStore,
    *,
    rows: bytes,
    import_key: str = "fixture",
    currency: str = "INR",
) -> tuple[str, ...]:
    result = import_eod_csv(
        store,
        HEADER + rows,
        import_key=import_key,
        source_id="fixture",
        source_url="https://example.test/data",
        review_url="https://example.test/terms",
        reviewed_at=datetime(2026, 11, 1, tzinfo=UTC),
        currency=currency,
        adjustment=Adjustment.UNADJUSTED,
    )
    return result.record_ids


def inspect(
    store: DataRecordStore,
    records: tuple[str, ...],
    *,
    as_of: datetime,
    dates: tuple[date, ...] = DATES,
    threshold: int = 10000,
) -> EodQualityReport:
    return inspect_eod_quality(
        store,
        source_id="fixture",
        instrument_id="TEST",
        adjustment=Adjustment.UNADJUSTED,
        expected_dates=dates,
        record_ids=records,
        as_of=as_of,
        max_change_basis_points=threshold,
    )


def test_quality_reports_missing_dates_and_exact_change_threshold_without_bridging_gaps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = Clock()
    store = DataRecordStore(path, clock)
    ids = import_rows(
        store,
        rows=(
            b"TEST,2026-11-17,2026-11-17T10:00:00Z,10,10,10,10,100\n"
            b"TEST,2026-11-18,2026-11-18T10:00:00Z,20,20,20,20,100\n"
            b"TEST,2026-11-20,2026-11-20T10:00:00Z,10,10,10,10,100\n"
            b"TEST,2026-11-21,2026-11-21T10:00:00Z,21,21,21,21,100\n"
        ),
    )
    report = inspect(store, tuple(reversed(ids)), as_of=clock.now())
    assert [item.record_id for item in report.observations] == [
        ids[0],
        ids[1],
        None,
        ids[2],
        ids[3],
    ]
    assert [item.close_outlier for item in report.observations] == [
        None,
        False,
        None,
        None,
        True,
    ]
    assert all(
        item.quality_status is QualityStatus.UNASSESSED
        for item in report.observations
        if item.record_id is not None
    )
    with pytest.raises(MissingDataRecord):
        inspect(store, ids, as_of=clock.now() - timedelta(microseconds=1))


def test_quality_rejects_duplicate_versions_and_mixed_units_across_gap(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = Clock()
    store = DataRecordStore(path, clock)
    first = import_rows(
        store, rows=b"TEST,2026-11-17,2026-11-17T10:00:00Z,10,10,10,10,100\n"
    )
    clock.instant += timedelta(seconds=1)
    second = import_rows(
        store,
        import_key="second",
        currency="USD",
        rows=b"TEST,2026-11-20,2026-11-20T10:00:00Z,10,10,10,10,100\n",
    )
    with pytest.raises(ValueError, match="same unit"):
        inspect(store, first + second, as_of=clock.now())
    with pytest.raises(ValueError, match="distinct"):
        inspect(store, first + first, as_of=clock.now())
    clock.instant += timedelta(seconds=1)
    correction = import_rows(
        store,
        import_key="correction",
        rows=b"TEST,2026-11-17,2026-11-17T10:00:00Z,11,11,11,11,100\n",
    )
    with pytest.raises(ValueError, match="requested series"):
        inspect(store, first + correction, as_of=clock.now())


def test_quality_rejects_invalid_scope_and_unexpected_records(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = Clock()
    store = DataRecordStore(path, clock)
    ids = import_rows(
        store, rows=b"TEST,2026-11-17,2026-11-17T10:00:00Z,10,10,10,10,100\n"
    )
    with pytest.raises(ValueError, match="ordered dates"):
        inspect(store, ids, as_of=clock.now(), dates=(DATES[1], DATES[0]))
    with pytest.raises(ValueError, match="basis points"):
        inspect(store, ids, as_of=clock.now(), threshold=-1)
    with pytest.raises(ValueError, match="timezone-aware"):
        inspect(store, ids, as_of=clock.now().replace(tzinfo=None))
    with pytest.raises(ValueError, match="requested series"):
        inspect(store, ids, as_of=clock.now(), dates=(DATES[1],))


def test_quality_limits_expected_dates_and_threshold(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = Clock()
    store = DataRecordStore(path, clock)
    dates = tuple(date(2025, 1, 1) + timedelta(days=index) for index in range(367))
    report = inspect(
        store, (), as_of=clock.now(), dates=dates[:366], threshold=1_000_000
    )
    assert len(report.observations) == 366
    assert all(item.record_id is None for item in report.observations)
    with pytest.raises(ValueError, match="ordered dates"):
        inspect(store, (), as_of=clock.now(), dates=dates)
    with pytest.raises(ValueError, match="basis points"):
        inspect(store, (), as_of=clock.now(), threshold=1_000_001)


def test_cli_inspects_one_import_and_reports_unavailable_or_incomplete_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    clock = Clock()
    store = DataRecordStore(path, clock)
    ids = import_rows(
        store,
        rows=(
            b"TEST,2026-11-17,2026-11-17T10:00:00Z,10,10,10,10,100\n"
            b"TEST,2026-11-18,2026-11-18T10:00:00Z,21,21,21,21,100\n"
        ),
    )
    with closing(sqlite3.connect(path)) as connection:
        import_id = connection.execute(
            "SELECT import_id FROM data_imports WHERE import_key = 'fixture'"
        ).fetchone()[0]
    clock.instant += timedelta(seconds=1)
    import_rows(
        store,
        import_key="other",
        rows=b"TEST,2026-11-19,2026-11-19T10:00:00Z,10,10,10,10,100\n",
    )
    args = [
        "data",
        "inspect-eod",
        import_id,
        "--source-id",
        "fixture",
        "--instrument-id",
        "TEST",
        "--adjustment",
        "UNADJUSTED",
        "--date",
        "2026-11-17",
        "--date",
        "2026-11-18",
        "--date",
        "2026-11-19",
        "--as-of",
        clock.now().isoformat(),
        "--max-change-bps",
        "10000",
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row["record_id"] for row in result["observations"]] == [*ids, None]
    assert result["observations"][1]["close_outlier"] is True
    assert result["observations"][0]["quality_status"] == "UNASSESSED"
    unavailable = args.copy()
    unavailable[unavailable.index("--as-of") + 1] = (
        clock.now() - timedelta(seconds=2)
    ).isoformat()
    assert main(unavailable) == 1
    assert "unavailable" in capsys.readouterr().err
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER data_import_records_no_delete")
        connection.execute(
            "DELETE FROM data_import_records WHERE import_id = ? AND ordinal = 1",
            (import_id,),
        )
    assert main(args) == 1
    assert "manifest is incomplete" in capsys.readouterr().err


def test_calendar_based_import_inspection_requires_coverage_and_closed_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    clock = Clock()
    store = DataRecordStore(path, clock)
    ids = import_rows(
        store,
        rows=(
            b"TEST,2026-11-17,2026-11-17T10:00:00Z,10,10,10,10,100\n"
            b"TEST,2026-11-18,2026-11-18T10:00:00Z,13,13,13,13,100\n"
            b"TEST,2026-11-19,2026-11-19T10:00:00Z,21,21,21,21,100\n"
        ),
    )
    with closing(sqlite3.connect(path)) as connection:
        import_id = connection.execute(
            "SELECT import_id FROM data_imports WHERE import_key = 'fixture'"
        ).fetchone()[0]
    calendar = ExchangeCalendar(path, clock)

    def record(day: date, state: SessionState) -> str:
        return calendar.record(
            CalendarSession(
                exchange_code="TEST",
                session_date=day,
                session_class=SessionClass.REGULAR
                if state is SessionState.OPEN
                else SessionClass.HOLIDAY,
                state=state,
                opens_at=datetime(day.year, day.month, day.day, 3, tzinfo=UTC)
                if state is SessionState.OPEN
                else None,
                closes_at=datetime(day.year, day.month, day.day, 10, tzinfo=UTC)
                if state is SessionState.OPEN
                else None,
                source_url="https://example.test/calendar",
                source_published_at=None,
                known_at=clock.now(),
                reviewed_by="owner",
                reviewed_at=clock.now(),
            )
        )

    session_ids = [
        record(DATES[0], SessionState.OPEN),
        record(DATES[1], SessionState.CLOSED),
    ]

    def inspect_calendar(
        *, start: date = DATES[0], end: date = DATES[2], as_of: datetime = clock.now()
    ) -> CalendarEodQualityReport:
        return inspect_eod_import_with_calendar(
            store,
            calendar,
            import_id,
            exchange_code="TEST",
            start=start,
            end=end,
            source_id="fixture",
            instrument_id="TEST",
            adjustment=Adjustment.UNADJUSTED,
            as_of=as_of,
            max_change_basis_points=10000,
        )

    with pytest.raises(UncoveredCalendarDate):
        inspect_calendar()
    session_ids.append(record(DATES[2], SessionState.OPEN))
    report = inspect_calendar()
    assert [item.session_id for item in report.calendar] == session_ids
    assert [item.record_id for item in report.quality.observations] == [ids[0], ids[2]]
    assert report.quality.observations[1].close_outlier is True
    assert [
        (item.session_date, item.record_id) for item in report.unexpected_closed_bars
    ] == [(DATES[1], ids[1])]
    session_ids.append(record(DATES[3], SessionState.OPEN))
    missing_report = inspect_calendar(end=DATES[3])
    assert [item.record_id for item in missing_report.quality.observations] == [
        ids[0],
        ids[2],
        None,
    ]
    with pytest.raises(UncoveredCalendarDate):
        inspect_calendar(as_of=clock.now() - timedelta(microseconds=1))
    future = date(2026, 11, 23)
    record(future, SessionState.OPEN)
    with pytest.raises(ValueError, match="has not closed"):
        inspect_calendar(start=future, end=future)
    with pytest.raises(ValueError, match="no open sessions"):
        inspect_calendar(start=DATES[1], end=DATES[1])

    args = [
        "data",
        "inspect-eod",
        import_id,
        "--source-id",
        "fixture",
        "--instrument-id",
        "TEST",
        "--adjustment",
        "UNADJUSTED",
        "--calendar-exchange",
        "TEST",
        "--calendar-start",
        DATES[0].isoformat(),
        "--calendar-end",
        DATES[3].isoformat(),
        "--as-of",
        clock.now().isoformat(),
        "--max-change-bps",
        "10000",
    ]
    assert main(args) == 1
    assert "--assume-open-sessions-expected" in capsys.readouterr().err
    assert main(args + ["--assume-open-sessions-expected"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert [item["session_id"] for item in output["calendar"]] == session_ids
    assert [item["record_id"] for item in output["quality"]["observations"]] == [
        ids[0],
        ids[2],
        None,
    ]
    assert output["unexpected_closed_bars"] == [
        {"session_date": DATES[1].isoformat(), "record_id": ids[1]}
    ]
    args[args.index("--calendar-end") + 1] = DATES[4].isoformat()
    assert main(args + ["--assume-open-sessions-expected"]) == 1
    assert "No reviewed calendar entry" in capsys.readouterr().err
