import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction

from bq.data.calendar import ExchangeCalendar, SessionState, UncoveredCalendarDate
from bq.data.csv_eod import INSTRUMENT_ID, PRICE, SOURCE_ID, Adjustment
from bq.data.records import DataRecordStore, QualityStatus, RecordKind

MAX_DATES = 366


@dataclass(frozen=True, slots=True)
class EodObservation:
    trading_date: date
    record_id: str | None
    quality_status: QualityStatus | None
    close_outlier: bool | None


@dataclass(frozen=True, slots=True)
class EodQualityReport:
    source_id: str
    instrument_id: str
    adjustment: Adjustment
    as_of: datetime
    max_change_basis_points: int
    observations: tuple[EodObservation, ...]


@dataclass(frozen=True, slots=True)
class CalendarEvidence:
    session_date: date
    session_id: str
    state: SessionState


@dataclass(frozen=True, slots=True)
class UnexpectedClosedBar:
    session_date: date
    record_id: str


@dataclass(frozen=True, slots=True)
class CalendarEodQualityReport:
    exchange_code: str
    calendar: tuple[CalendarEvidence, ...]
    quality: EodQualityReport
    unexpected_closed_bars: tuple[UnexpectedClosedBar, ...]


def inspect_eod_import_with_calendar(
    store: DataRecordStore,
    calendar: ExchangeCalendar,
    import_id: str,
    *,
    exchange_code: str,
    start: date,
    end: date,
    source_id: str,
    instrument_id: str,
    adjustment: Adjustment,
    as_of: datetime,
    max_change_basis_points: int,
) -> CalendarEodQualityReport:
    coverage = calendar.coverage(exchange_code, start=start, end=end, as_of=as_of)
    evidence: list[CalendarEvidence] = []
    expected_dates: list[date] = []
    closed_dates: list[date] = []
    for day in coverage:
        version = day.version
        if version is None:
            raise UncoveredCalendarDate(
                f"No reviewed calendar entry for {exchange_code} on {day.session_date}."
            )
        session = version.session
        evidence.append(
            CalendarEvidence(day.session_date, version.session_id, session.state)
        )
        if session.state is SessionState.OPEN:
            if session.closes_at is None or session.closes_at > as_of:
                raise ValueError(
                    f"Open session on {day.session_date} has not closed by as_of."
                )
            expected_dates.append(day.session_date)
        else:
            closed_dates.append(day.session_date)
    if not expected_dates:
        raise ValueError("Calendar range contains no open sessions to inspect.")
    quality = inspect_eod_import(
        store,
        import_id,
        source_id=source_id,
        instrument_id=instrument_id,
        adjustment=adjustment,
        expected_dates=tuple(expected_dates),
        as_of=as_of,
        max_change_basis_points=max_change_basis_points,
    )
    unexpected: tuple[UnexpectedClosedBar, ...] = ()
    if closed_dates:
        keys = tuple(
            f"{source_id}/{instrument_id}/{day.isoformat()}/{adjustment.value}"
            for day in closed_dates
        )
        ids = store.import_records_for_keys(
            import_id, source_id=source_id, record_keys=keys, as_of=as_of
        )
        closed_quality = inspect_eod_quality(
            store,
            source_id=source_id,
            instrument_id=instrument_id,
            adjustment=adjustment,
            expected_dates=tuple(closed_dates),
            record_ids=ids,
            as_of=as_of,
            max_change_basis_points=max_change_basis_points,
        )
        unexpected = tuple(
            UnexpectedClosedBar(item.trading_date, item.record_id)
            for item in closed_quality.observations
            if item.record_id is not None
        )
    return CalendarEodQualityReport(exchange_code, tuple(evidence), quality, unexpected)


def _price(value: object) -> Decimal:
    if not isinstance(value, str) or len(value) > 64 or PRICE.fullmatch(value) is None:
        raise ValueError("EOD prices must be positive decimal strings.")
    result = Decimal(value)
    if result <= 0:
        raise ValueError("EOD prices must be positive decimal strings.")
    return result


def _validate_scope(
    *,
    source_id: str,
    instrument_id: str,
    adjustment: Adjustment,
    expected_dates: tuple[date, ...],
    as_of: datetime,
    max_change_basis_points: int,
) -> None:
    if not isinstance(source_id, str) or SOURCE_ID.fullmatch(source_id) is None:
        raise ValueError("Invalid EOD source identifier.")
    if (
        not isinstance(instrument_id, str)
        or INSTRUMENT_ID.fullmatch(instrument_id) is None
    ):
        raise ValueError("Invalid EOD instrument identifier.")
    if not isinstance(adjustment, Adjustment):
        raise ValueError("Invalid EOD adjustment mode.")
    if (
        not isinstance(expected_dates, tuple)
        or not 1 <= len(expected_dates) <= MAX_DATES
        or any(
            not isinstance(day, date) or isinstance(day, datetime)
            for day in expected_dates
        )
        or any(left >= right for left, right in zip(expected_dates, expected_dates[1:]))
    ):
        raise ValueError("Expected dates must be 1 to 366 distinct ordered dates.")
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("EOD as_of must be timezone-aware.")
    if (
        isinstance(max_change_basis_points, bool)
        or not isinstance(max_change_basis_points, int)
        or not 0 <= max_change_basis_points <= 1_000_000
    ):
        raise ValueError("Maximum change must be between 0 and 1000000 basis points.")


def inspect_eod_import(
    store: DataRecordStore,
    import_id: str,
    *,
    source_id: str,
    instrument_id: str,
    adjustment: Adjustment,
    expected_dates: tuple[date, ...],
    as_of: datetime,
    max_change_basis_points: int,
) -> EodQualityReport:
    _validate_scope(
        source_id=source_id,
        instrument_id=instrument_id,
        adjustment=adjustment,
        expected_dates=expected_dates,
        as_of=as_of,
        max_change_basis_points=max_change_basis_points,
    )
    keys = tuple(
        f"{source_id}/{instrument_id}/{day.isoformat()}/{adjustment.value}"
        for day in expected_dates
    )
    ids = store.import_records_for_keys(
        import_id, source_id=source_id, record_keys=keys, as_of=as_of
    )
    return inspect_eod_quality(
        store,
        source_id=source_id,
        instrument_id=instrument_id,
        adjustment=adjustment,
        expected_dates=expected_dates,
        record_ids=ids,
        as_of=as_of,
        max_change_basis_points=max_change_basis_points,
    )


def inspect_eod_quality(
    store: DataRecordStore,
    *,
    source_id: str,
    instrument_id: str,
    adjustment: Adjustment,
    expected_dates: tuple[date, ...],
    record_ids: tuple[str, ...],
    as_of: datetime,
    max_change_basis_points: int,
) -> EodQualityReport:
    _validate_scope(
        source_id=source_id,
        instrument_id=instrument_id,
        adjustment=adjustment,
        expected_dates=expected_dates,
        as_of=as_of,
        max_change_basis_points=max_change_basis_points,
    )
    if (
        not isinstance(record_ids, tuple)
        or len(record_ids) > len(expected_dates)
        or any(not isinstance(item, str) for item in record_ids)
        or len(set(record_ids)) != len(record_ids)
    ):
        raise ValueError("Record IDs must be distinct and no more numerous than dates.")
    expected = set(expected_dates)
    by_date: dict[date, tuple[str, QualityStatus, Fraction, str]] = {}
    for record_id in record_ids:
        version = store.version_for(record_id, as_of=as_of)
        record = version.record
        payload = record.payload
        trading_date_text = payload.get("trading_date")
        if not isinstance(trading_date_text, str):
            raise ValueError("EOD record has an invalid trading date.")
        try:
            trading_date = date.fromisoformat(trading_date_text)
        except ValueError as exc:
            raise ValueError("EOD record has an invalid trading date.") from exc
        if (
            trading_date.isoformat() != trading_date_text
            or trading_date not in expected
            or trading_date in by_date
            or record.kind is not RecordKind.EOD_BAR
            or record.source_id != source_id
            or record.record_key
            != f"{source_id}/{instrument_id}/{trading_date_text}/{adjustment.value}"
            or payload.get("instrument_id") != instrument_id
            or payload.get("adjustment") != adjustment.value
            or record.unit is None
            or re.fullmatch(r"[A-Z]{3}/share", record.unit) is None
            or record.event_at is None
            or record.event_at > record.received_at
        ):
            raise ValueError("EOD record does not match the requested series.")
        opening, high, low, close_price = (
            _price(payload.get(field)) for field in ("open", "high", "low", "close")
        )
        volume = payload.get("volume")
        if (
            high < max(opening, low, close_price)
            or low > min(opening, high, close_price)
            or isinstance(volume, bool)
            or not isinstance(volume, int)
            or not 0 <= volume <= 9223372036854775807
        ):
            raise ValueError("EOD record has invalid OHLC ordering or volume.")
        by_date[trading_date] = (
            record_id,
            record.quality_status,
            Fraction(close_price),
            record.unit,
        )

    observations: list[EodObservation] = []
    previous_close: Fraction | None = None
    series_unit: str | None = None
    for trading_date in expected_dates:
        item = by_date.get(trading_date)
        if item is None:
            observations.append(EodObservation(trading_date, None, None, None))
            previous_close = None
            continue
        record_id, quality, current_close, unit = item
        if series_unit is not None and series_unit != unit:
            raise ValueError("EOD records in a series must use the same unit.")
        outlier = (
            abs(current_close - previous_close) * 10_000
            > previous_close * max_change_basis_points
            if previous_close is not None
            else None
        )
        observations.append(EodObservation(trading_date, record_id, quality, outlier))
        previous_close, series_unit = current_close, unit
    return EodQualityReport(
        source_id,
        instrument_id,
        adjustment,
        as_of,
        max_change_basis_points,
        tuple(observations),
    )
