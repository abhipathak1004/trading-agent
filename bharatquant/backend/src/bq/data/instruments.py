import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from bq.data.calendar import EXCHANGE_CODE
from bq.data.records import DataRecord, DataRecordStore, QualityStatus, RecordKind

SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_INTERVALS = 256


class MissingSymbolMapping(LookupError):
    pass


def _date(value: date, label: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{label} must be a date.")


@dataclass(frozen=True, slots=True)
class SymbolInterval:
    instrument_id: str
    effective_from: date
    effective_until: date | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.instrument_id, str)
            or SYMBOL.fullmatch(self.instrument_id) is None
        ):
            raise ValueError("Invalid instrument identifier.")
        _date(self.effective_from, "Effective start")
        if self.effective_until is not None:
            _date(self.effective_until, "Effective end")
            if self.effective_until <= self.effective_from:
                raise ValueError("Effective end must be after its start.")


@dataclass(frozen=True, slots=True)
class SymbolTimeline:
    exchange_code: str
    symbol: str
    intervals: tuple[SymbolInterval, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.exchange_code, str)
            or EXCHANGE_CODE.fullmatch(self.exchange_code) is None
        ):
            raise ValueError("Invalid exchange code.")
        if not isinstance(self.symbol, str) or SYMBOL.fullmatch(self.symbol) is None:
            raise ValueError("Invalid symbol.")
        if (
            not isinstance(self.intervals, tuple)
            or len(self.intervals) > MAX_INTERVALS
            or any(not isinstance(item, SymbolInterval) for item in self.intervals)
        ):
            raise ValueError(
                "Symbol timeline must contain at most 256 valid intervals."
            )
        for previous, current in zip(self.intervals, self.intervals[1:]):
            if (
                previous.effective_until is None
                or current.effective_from < previous.effective_until
            ):
                raise ValueError(
                    "Symbol intervals must be ordered and non-overlapping."
                )

    @property
    def record_key(self) -> str:
        return f"symbol:{self.exchange_code}:{self.symbol}"


@dataclass(frozen=True, slots=True)
class ResolvedSymbol:
    instrument_id: str
    record_id: str
    recorded_at: datetime
    known_at: datetime
    source_id: str
    quality_status: QualityStatus


@dataclass(frozen=True, slots=True)
class SymbolSourceFinding:
    source_id: str
    record_id: str | None
    instrument_id: str | None
    quality_status: QualityStatus | None


@dataclass(frozen=True, slots=True)
class SymbolSourceComparison:
    exchange_code: str
    symbol: str
    effective_on: date
    as_of: datetime
    findings: tuple[SymbolSourceFinding, ...]
    has_uncovered_sources: bool
    has_conflicting_instruments: bool


def record_symbol_timeline(
    store: DataRecordStore,
    timeline: SymbolTimeline,
    *,
    source_id: str,
    source_url: str,
    known_at: datetime,
    received_at: datetime,
    published_at: datetime | None = None,
) -> str:
    record = DataRecord.create(
        kind=RecordKind.INSTRUMENT,
        record_key=timeline.record_key,
        source_id=source_id,
        source_url=source_url,
        known_at=known_at,
        received_at=received_at,
        published_at=published_at,
        payload={
            "schema": "symbol-timeline-v1",
            "exchange_code": timeline.exchange_code,
            "symbol": timeline.symbol,
            "intervals": [
                {
                    "instrument_id": interval.instrument_id,
                    "effective_from": interval.effective_from.isoformat(),
                    "effective_until": (
                        interval.effective_until.isoformat()
                        if interval.effective_until is not None
                        else None
                    ),
                }
                for interval in timeline.intervals
            ],
        },
    )
    return store.record(record)


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("Expected an ISO date.")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Expected a canonical ISO date.")
    return parsed


def _timeline(record: DataRecord) -> SymbolTimeline:
    try:
        payload = record.payload
        if set(payload) != {"schema", "exchange_code", "symbol", "intervals"}:
            raise ValueError("Invalid timeline fields.")
        if payload["schema"] != "symbol-timeline-v1":
            raise ValueError("Unknown timeline schema.")
        exchange = payload["exchange_code"]
        symbol = payload["symbol"]
        intervals = payload["intervals"]
        if not isinstance(exchange, str) or not isinstance(symbol, str):
            raise ValueError("Invalid symbol identity.")
        if not isinstance(intervals, list) or len(intervals) > MAX_INTERVALS:
            raise ValueError("Invalid intervals.")
        parsed = []
        for item in intervals:
            if not isinstance(item, dict) or set(item) != {
                "instrument_id",
                "effective_from",
                "effective_until",
            }:
                raise ValueError("Invalid interval fields.")
            instrument = item["instrument_id"]
            if not isinstance(instrument, str):
                raise ValueError("Invalid instrument identifier.")
            end = item["effective_until"]
            parsed.append(
                SymbolInterval(
                    instrument,
                    _parse_date(item["effective_from"]),
                    _parse_date(end) if end is not None else None,
                )
            )
        timeline = SymbolTimeline(exchange, symbol, tuple(parsed))
        if (
            record.kind is not RecordKind.INSTRUMENT
            or record.record_key != timeline.record_key
        ):
            raise ValueError("Timeline identity does not match its record.")
        return timeline
    except (TypeError, ValueError) as exc:
        raise sqlite3.DatabaseError("Invalid stored symbol timeline.") from exc


def resolve_symbol(
    store: DataRecordStore,
    *,
    exchange_code: str,
    symbol: str,
    effective_on: date,
    as_of: datetime,
) -> ResolvedSymbol:
    _date(effective_on, "Effective date")
    key = SymbolTimeline(exchange_code, symbol, ()).record_key
    version = store.record_version_for(RecordKind.INSTRUMENT, key, as_of=as_of)
    timeline = _timeline(version.record)
    instrument_id = _instrument_on(timeline, effective_on)
    if instrument_id is not None:
        return ResolvedSymbol(
            instrument_id,
            version.record_id,
            version.recorded_at,
            version.record.known_at,
            version.record.source_id,
            version.record.quality_status,
        )
    raise MissingSymbolMapping(
        f"No mapping for {exchange_code}:{symbol} on {effective_on.isoformat()}."
    )


def _instrument_on(timeline: SymbolTimeline, effective_on: date) -> str | None:
    for interval in timeline.intervals:
        if interval.effective_from <= effective_on and (
            interval.effective_until is None or effective_on < interval.effective_until
        ):
            return interval.instrument_id
    return None


def compare_symbol_sources(
    store: DataRecordStore,
    *,
    exchange_code: str,
    symbol: str,
    effective_on: date,
    as_of: datetime,
    source_ids: tuple[str, ...],
) -> SymbolSourceComparison:
    _date(effective_on, "Effective date")
    key = SymbolTimeline(exchange_code, symbol, ()).record_key
    versions = store.versions_for_sources(
        RecordKind.INSTRUMENT, key, source_ids=source_ids, as_of=as_of
    )
    findings = tuple(
        SymbolSourceFinding(
            source_id,
            version.record_id if version is not None else None,
            _instrument_on(_timeline(version.record), effective_on)
            if version is not None
            else None,
            version.record.quality_status if version is not None else None,
        )
        for source_id, version in zip(source_ids, versions, strict=True)
    )
    return SymbolSourceComparison(
        exchange_code,
        symbol,
        effective_on,
        as_of,
        findings,
        any(item.instrument_id is None for item in findings),
        len({item.instrument_id for item in findings if item.instrument_id is not None})
        > 1,
    )
