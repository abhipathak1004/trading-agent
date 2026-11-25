import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from bq.cli import main
from bq.data import records
from bq.data.instruments import (
    MissingSymbolMapping,
    ResolvedSymbol,
    SymbolInterval,
    SymbolSourceComparison,
    SymbolTimeline,
    compare_symbol_sources,
    record_symbol_timeline,
    resolve_symbol,
)
from bq.data.records import (
    DataRecord,
    DataRecordStore,
    MissingDataRecord,
    QualityStatus,
    RecordKind,
)
from bq.infra.database import migrate


class Clock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


def save(store: DataRecordStore, timeline: SymbolTimeline, known_at: datetime) -> str:
    return record_symbol_timeline(
        store,
        timeline,
        source_id="fixture",
        source_url="https://example.test/data",
        known_at=known_at,
        received_at=known_at,
    )


def test_mapping_resolves_effective_boundaries_and_retains_as_of_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = Clock()
    store = DataRecordStore(path, clock)
    timeline = SymbolTimeline(
        "TEST",
        "ABC",
        (
            SymbolInterval("old", date(2026, 1, 1), date(2026, 6, 1)),
            SymbolInterval("new", date(2026, 6, 1), None),
        ),
    )
    first_id = save(store, timeline, clock.now() - timedelta(hours=1))
    first_recorded_at = clock.now()

    def lookup(day: date, as_of: datetime) -> ResolvedSymbol:
        return resolve_symbol(
            store, exchange_code="TEST", symbol="ABC", effective_on=day, as_of=as_of
        )

    with pytest.raises(MissingDataRecord):
        lookup(date(2026, 5, 31), first_recorded_at - timedelta(microseconds=1))
    with pytest.raises(MissingSymbolMapping):
        lookup(date(2025, 12, 31), clock.now())
    old = lookup(date(2026, 5, 31), clock.now())
    assert old.instrument_id == "old"
    assert old.record_id == first_id
    assert old.recorded_at == first_recorded_at
    assert old.quality_status is QualityStatus.UNASSESSED
    assert lookup(date(2026, 6, 1), clock.now()).instrument_id == "new"

    clock.instant += timedelta(seconds=1)
    corrected = SymbolTimeline(
        "TEST", "ABC", (SymbolInterval("corrected", date(2026, 1, 1), None),)
    )
    correction_id = save(store, corrected, clock.now())
    assert lookup(date(2026, 6, 1), first_recorded_at).record_id == first_id
    current = lookup(date(2026, 6, 1), clock.now())
    assert (current.instrument_id, current.record_id) == ("corrected", correction_id)
    assert (
        store.version_for(first_id, as_of=clock.now()).record.payload["symbol"] == "ABC"
    )


def test_mapping_rejects_overlaps_and_invalid_intervals() -> None:
    first = SymbolInterval("one", date(2026, 1, 1), date(2026, 3, 1))
    with pytest.raises(ValueError, match="non-overlapping"):
        SymbolTimeline(
            "TEST", "ABC", (first, SymbolInterval("two", date(2026, 2, 1), None))
        )
    with pytest.raises(ValueError, match="non-overlapping"):
        SymbolTimeline(
            "TEST", "ABC", (SymbolInterval("one", date(2026, 1, 1), None), first)
        )
    with pytest.raises(ValueError, match="after its start"):
        SymbolInterval("one", date(2026, 3, 1), date(2026, 3, 1))
    with pytest.raises(ValueError, match="at most"):
        SymbolTimeline("TEST", "ABC", (first,) * 257)


def test_mapping_gaps_and_malformed_latest_version_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = Clock()
    store = DataRecordStore(path, clock)
    timeline = SymbolTimeline(
        "TEST",
        "ABC",
        (
            SymbolInterval("one", date(2026, 1, 1), date(2026, 3, 1)),
            SymbolInterval("two", date(2026, 4, 1), None),
        ),
    )
    save(store, timeline, clock.now())
    with pytest.raises(MissingSymbolMapping):
        resolve_symbol(
            store,
            exchange_code="TEST",
            symbol="ABC",
            effective_on=date(2026, 3, 1),
            as_of=clock.now(),
        )
    clock.instant += timedelta(seconds=1)
    store.record(
        DataRecord.create(
            kind=RecordKind.INSTRUMENT,
            record_key=timeline.record_key,
            source_id="fixture",
            source_url="https://example.test/data",
            known_at=clock.now(),
            received_at=clock.now(),
            payload={"schema": "unknown"},
        )
    )
    with pytest.raises(sqlite3.DatabaseError, match="Invalid stored symbol timeline"):
        resolve_symbol(
            store,
            exchange_code="TEST",
            symbol="ABC",
            effective_on=date(2026, 2, 1),
            as_of=clock.now(),
        )


def test_source_comparison_reports_conflict_and_uncovered_without_trusting_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = Clock()
    store = DataRecordStore(path, clock)
    effective = date(2026, 6, 1)

    def record(source: str, instrument: str | None) -> str:
        timeline = SymbolTimeline(
            "TEST",
            "ABC",
            (SymbolInterval(instrument, date(2026, 1, 1), None),) if instrument else (),
        )
        return record_symbol_timeline(
            store,
            timeline,
            source_id=source,
            source_url="https://example.test/data",
            known_at=clock.now(),
            received_at=clock.now(),
        )

    first = record("first", "one")
    prior = clock.now()
    clock.instant += timedelta(seconds=1)
    second = record("second", "two")
    clock.instant += timedelta(seconds=1)
    uncovered = record("third", None)
    sources = ("first", "second", "third", "missing")

    def compare(as_of: datetime) -> SymbolSourceComparison:
        return compare_symbol_sources(
            store,
            exchange_code="TEST",
            symbol="ABC",
            effective_on=effective,
            as_of=as_of,
            source_ids=sources,
        )

    report = compare(clock.now())
    assert [item.record_id for item in report.findings] == [
        first,
        second,
        uncovered,
        None,
    ]
    assert [item.instrument_id for item in report.findings] == [
        "one",
        "two",
        None,
        None,
    ]
    assert report.has_uncovered_sources and report.has_conflicting_instruments
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    args = [
        "data",
        "inspect-symbol",
        "--exchange-code",
        "TEST",
        "--symbol",
        "ABC",
        "--effective-on",
        effective.isoformat(),
        "--as-of",
        clock.now().isoformat(),
    ]
    for source in sources:
        args += ["--source-id", source]
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["has_conflicting_instruments"] and output["has_uncovered_sources"]
    assert [item["record_id"] for item in output["findings"]] == [
        first,
        second,
        uncovered,
        None,
    ]
    assert all(
        item.quality_status is QualityStatus.UNASSESSED
        for item in report.findings
        if item.record_id is not None
    )
    earlier = compare(prior)
    assert [item.record_id for item in earlier.findings] == [first, None, None, None]
    assert earlier.has_uncovered_sources and not earlier.has_conflicting_instruments

    clock.instant += timedelta(seconds=1)
    corrected = record("second", "one")
    report = compare(clock.now())
    assert report.findings[1].record_id == corrected
    assert not report.has_conflicting_instruments and report.has_uncovered_sources
    with pytest.raises(ValueError, match="distinct data source"):
        compare_symbol_sources(
            store,
            exchange_code="TEST",
            symbol="ABC",
            effective_on=effective,
            as_of=clock.now(),
            source_ids=("first", "first"),
        )
    with pytest.raises(ValueError, match="distinct data source"):
        compare_symbol_sources(
            store,
            exchange_code="TEST",
            symbol="ABC",
            effective_on=effective,
            as_of=clock.now(),
            source_ids=tuple(f"source{i}" for i in range(33)),
        )
    clock.instant += timedelta(seconds=1)
    store.record(
        DataRecord.create(
            kind=RecordKind.INSTRUMENT,
            record_key="symbol:TEST:ABC",
            source_id="second",
            source_url="https://example.test/data",
            known_at=clock.now(),
            received_at=clock.now(),
            payload={"schema": "unknown"},
        )
    )
    with pytest.raises(sqlite3.DatabaseError, match="Invalid stored symbol timeline"):
        compare(clock.now())


def test_source_comparison_uses_one_snapshot_during_concurrent_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = Clock()
    store = DataRecordStore(path, clock)
    writer = DataRecordStore(path, clock)
    timeline = SymbolTimeline(
        "TEST", "ABC", (SymbolInterval("one", date(2026, 1, 1), None),)
    )
    first = record_symbol_timeline(
        store,
        timeline,
        source_id="first",
        source_url="https://example.test/data",
        known_at=clock.now(),
        received_at=clock.now(),
    )
    clock.instant += timedelta(seconds=1)
    decoder = records._decode_version
    written: list[str] = []

    def decode_and_record(
        record_id: str, row: tuple[object, ...]
    ) -> records.DataRecordVersion:
        version = decoder(record_id, row)
        if not written:
            written.append(
                record_symbol_timeline(
                    writer,
                    timeline,
                    source_id="second",
                    source_url="https://example.test/data",
                    known_at=clock.now(),
                    received_at=clock.now(),
                )
            )
        return version

    monkeypatch.setattr(records, "_decode_version", decode_and_record)
    report = compare_symbol_sources(
        store,
        exchange_code="TEST",
        symbol="ABC",
        effective_on=date(2026, 6, 1),
        as_of=clock.now(),
        source_ids=("first", "second"),
    )
    assert written
    assert [item.record_id for item in report.findings] == [first, None]
    assert report.has_uncovered_sources
    updated = compare_symbol_sources(
        store,
        exchange_code="TEST",
        symbol="ABC",
        effective_on=date(2026, 6, 1),
        as_of=clock.now(),
        source_ids=("first", "second"),
    )
    assert [item.record_id for item in updated.findings] == [first, written[0]]
    assert not updated.has_uncovered_sources and not updated.has_conflicting_instruments
