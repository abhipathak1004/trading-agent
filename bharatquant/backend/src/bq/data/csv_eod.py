import csv
import io
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from urllib.parse import urlsplit

from bq.data.records import DataImportResult, DataRecord, DataRecordStore, RecordKind

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10000
HEADER = (
    "instrument_id",
    "trading_date",
    "event_at",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
INSTRUMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PRICE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class Adjustment(StrEnum):
    ADJUSTED = "ADJUSTED"
    UNADJUSTED = "UNADJUSTED"


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("CSV timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _https_url(value: str, label: str) -> None:
    source = urlsplit(value)
    if (
        source.scheme != "https"
        or not source.hostname
        or source.username is not None
        or source.password is not None
        or source.query
        or source.fragment
    ):
        raise ValueError(
            f"{label} URL must be HTTPS without credentials, query or fragment."
        )


def _price(value: str, line: int) -> Decimal:
    if len(value) > 64 or PRICE.fullmatch(value) is None:
        raise ValueError(f"CSV line {line}: invalid price.")
    result = Decimal(value)
    if result <= 0:
        raise ValueError(f"CSV line {line}: price must be positive.")
    return result


def import_eod_csv(
    store: DataRecordStore,
    content: bytes,
    *,
    import_key: str,
    source_id: str,
    source_url: str,
    review_url: str,
    reviewed_at: datetime,
    currency: str,
    adjustment: Adjustment,
) -> DataImportResult:
    if not isinstance(content, bytes):
        raise TypeError("CSV content must be bytes.")
    if len(content) > MAX_BYTES:
        raise ValueError("CSV exceeds the 5 MiB import limit.")
    if SOURCE_ID.fullmatch(source_id) is None:
        raise ValueError("Invalid CSV source identifier.")
    _https_url(source_url, "Source")
    _https_url(review_url, "Review")
    review_time = _utc(reviewed_at)
    if re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise ValueError("Currency must be a three-letter uppercase code.")
    if not isinstance(adjustment, Adjustment):
        raise ValueError("CSV adjustment mode must be explicit.")
    received_at = _utc(store.clock.now())
    if review_time > received_at:
        raise ValueError("Source review time cannot be in the future.")

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be encoded as UTF-8.") from exc

    records: list[DataRecord] = []
    seen: set[str] = set()
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        if tuple(next(reader, ())) != HEADER:
            raise ValueError("CSV header does not match the required EOD format.")
        for row in reader:
            line = reader.line_num
            if len(records) >= MAX_ROWS:
                raise ValueError("CSV exceeds the 10000-row import limit.")
            if len(row) != len(HEADER):
                raise ValueError(f"CSV line {line}: incorrect number of columns.")
            instrument_id, trading_date_text, event_text, *values = row
            if INSTRUMENT_ID.fullmatch(instrument_id) is None:
                raise ValueError(f"CSV line {line}: invalid instrument identifier.")
            try:
                trading_date = date.fromisoformat(trading_date_text)
                event_at = _utc(
                    datetime.fromisoformat(event_text.replace("Z", "+00:00"))
                )
            except ValueError as exc:
                raise ValueError(
                    f"CSV line {line}: invalid date or event time."
                ) from exc
            if trading_date.isoformat() != trading_date_text:
                raise ValueError(f"CSV line {line}: date must use YYYY-MM-DD.")
            if event_at > received_at:
                raise ValueError(f"CSV line {line}: event time is in the future.")
            open_price, high, low, close = (_price(value, line) for value in values[:4])
            if high < max(open_price, low, close) or low > min(open_price, high, close):
                raise ValueError(f"CSV line {line}: invalid OHLC ordering.")
            volume_text = values[4]
            if (
                re.fullmatch(r"[0-9]{1,19}", volume_text) is None
                or int(volume_text) > 9223372036854775807
            ):
                raise ValueError(f"CSV line {line}: invalid whole-share volume.")
            key = f"{source_id}/{instrument_id}/{trading_date}/{adjustment.value}"
            if key in seen:
                raise ValueError(f"CSV line {line}: duplicate instrument and date.")
            seen.add(key)
            records.append(
                DataRecord.create(
                    kind=RecordKind.EOD_BAR,
                    record_key=key,
                    source_id=source_id,
                    source_url=source_url,
                    unit=f"{currency}/share",
                    event_at=event_at,
                    known_at=received_at,
                    received_at=received_at,
                    payload={
                        "instrument_id": instrument_id,
                        "trading_date": trading_date_text,
                        "adjustment": adjustment.value,
                        "open": str(open_price),
                        "high": str(high),
                        "low": str(low),
                        "close": str(close),
                        "volume": int(volume_text),
                    },
                )
            )
    except csv.Error as exc:
        raise ValueError("Invalid CSV syntax.") from exc
    if not records:
        raise ValueError("CSV must contain at least one EOD row.")

    request = {
        "format": "eod-csv-v1",
        "content_hash": sha256(content).hexdigest(),
        "source_id": source_id,
        "source_url": source_url,
        "review_url": review_url,
        "reviewed_at": review_time.isoformat(timespec="microseconds"),
        "currency": currency,
        "adjustment": adjustment.value,
    }
    request_hash = sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return store._record_import(
        import_key,
        request_hash,
        tuple(records),
        source_id=source_id,
        review_url=review_url,
        reviewed_at=review_time,
    )
