"""Live EOD bars from Yahoo Finance.

Verified live against ``query1.finance.yahoo.com/v8/finance/chart/<symbol>``.
Indian equities use the ``.NS`` (NSE) or ``.BO`` (BSE) suffix.

What this does:

* Fetches daily bars for one symbol over a bounded range.
* Normalises nulls (Yahoo returns ``null`` for the current, not-yet-closed day)
  by dropping incomplete rows rather than storing zeros.
* Timestamps are epoch seconds in UTC. A bar's ``event_at`` is that instant.
* Rows are written through ``DataRecordStore`` as ``EOD_BAR`` records with a
  deterministic record key, so re-running is idempotent per key/version.

Honesty about the source: Yahoo is not an authoritative exchange feed. The
records are stored ``UNASSESSED`` and the source identity says so. The adapter
never claims the data is verified or suitable for a readiness gate.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

import httpx

from bq.core import Clock, SystemClock
from bq.data.records import DataImportResult, DataRecord, DataRecordStore, RecordKind

YAHOO_ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SOURCE_ID = "yahoo.finance"
SOURCE_URL = "https://finance.yahoo.com"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_SYMBOL_CHARS = 24
_USER_AGENT = "Mozilla/5.0 BharatQuant/0.1 (personal research)"

_VALID_RANGES = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max"}
_VALID_INTERVALS = {"1d", "1wk", "1mo"}


class YahooError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class YahooBar:
    trading_date: str
    event_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class YahooImportResult:
    import_id: str
    record_count: int
    symbol: str
    records: tuple[DataRecord, ...]


def _decimal(value: object) -> Decimal | None:
    # Yahoo values are floats; convert through str to keep stable decimals.
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return Decimal(str(value))


def _validate_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if not 1 <= len(value) <= MAX_SYMBOL_CHARS or not all(
        char.isalnum() or char in ".-^=" for char in value
    ):
        raise YahooError("Invalid Yahoo symbol.")
    return value


def parse_chart(
    payload: bytes, *, received_at: datetime
) -> tuple[str, str, tuple[YahooBar, ...]]:
    """Turn a Yahoo chart JSON body into (symbol, currency, complete bars)."""
    try:
        body = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise YahooError("Yahoo response was not valid JSON.") from exc
    chart = body.get("chart") if isinstance(body, dict) else None
    if not isinstance(chart, dict):
        raise YahooError("Yahoo response has no chart object.")
    if chart.get("error"):
        raise YahooError("Yahoo reported an error for this symbol.")
    results = chart.get("result")
    if not isinstance(results, list) or not results:
        raise YahooError("Yahoo returned no chart data.")
    first = results[0]
    if not isinstance(first, dict):
        raise YahooError("Yahoo chart result is malformed.")
    meta = first.get("meta")
    if not isinstance(meta, dict):
        raise YahooError("Yahoo chart meta is missing.")
    symbol = str(meta.get("symbol") or "")
    currency = str(meta.get("currency") or "")
    timestamps = first.get("timestamp")
    indicators = first.get("indicators")
    if (
        not isinstance(timestamps, list)
        or not isinstance(indicators, dict)
        or not isinstance(indicators.get("quote"), list)
        or not indicators["quote"]
    ):
        raise YahooError("Yahoo chart has no time series.")
    quote = indicators["quote"][0]
    if not isinstance(quote, dict):
        raise YahooError("Yahoo quote block is malformed.")
    raw_opens = quote.get("open")
    raw_highs = quote.get("high")
    raw_lows = quote.get("low")
    raw_closes = quote.get("close")
    raw_volumes = quote.get("volume")
    series = (raw_opens, raw_highs, raw_lows, raw_closes, raw_volumes)
    if not all(isinstance(item, list) for item in series):
        raise YahooError("Yahoo quote series are malformed.")
    opens: list[object] = raw_opens  # type: ignore[assignment]
    highs: list[object] = raw_highs  # type: ignore[assignment]
    lows: list[object] = raw_lows  # type: ignore[assignment]
    closes: list[object] = raw_closes  # type: ignore[assignment]
    volumes: list[object] = raw_volumes  # type: ignore[assignment]
    length = min(
        len(timestamps), len(opens), len(highs), len(lows), len(closes), len(volumes)
    )
    bars: list[YahooBar] = []
    for index in range(length):
        stamp = timestamps[index]
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            continue
        event_at = datetime.fromtimestamp(stamp, tz=UTC)
        if event_at > received_at.astimezone(UTC):
            continue
        open_p = _decimal(opens[index])
        high_p = _decimal(highs[index])
        low_p = _decimal(lows[index])
        close_p = _decimal(closes[index])
        volume = volumes[index]
        if None in (open_p, high_p, low_p, close_p) or volume is None:
            continue  # incomplete/current session
        if not isinstance(volume, int) or volume < 0:
            continue
        if any(
            price is None or price <= 0 for price in (open_p, high_p, low_p, close_p)
        ):
            continue
        assert open_p is not None and high_p is not None
        assert low_p is not None and close_p is not None
        if high_p < max(open_p, low_p, close_p) or low_p > min(open_p, high_p, close_p):
            continue
        bars.append(
            YahooBar(
                trading_date=event_at.date().isoformat(),
                event_at=event_at,
                open=open_p,
                high=high_p,
                low=low_p,
                close=close_p,
                volume=volume,
            )
        )
    if not bars:
        raise YahooError("Yahoo returned no complete bars for this symbol.")
    return symbol, currency, tuple(bars)


def fetch_chart(
    symbol: str,
    *,
    range_: str = "1mo",
    interval: str = "1d",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    if range_ not in _VALID_RANGES:
        raise YahooError("Unsupported Yahoo range.")
    if interval not in _VALID_INTERVALS:
        raise YahooError("Unsupported Yahoo interval.")
    url = YAHOO_ENDPOINT.format(symbol=quote(_validate_symbol(symbol)))
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            with client.stream(
                "GET",
                url,
                params={"range": range_, "interval": interval},
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise YahooError("Yahoo response exceeded the size limit.")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise YahooError(f"Yahoo request failed: {type(exc).__name__}.") from exc


def import_bars(
    store: DataRecordStore,
    *,
    symbol: str,
    bars: tuple[YahooBar, ...],
    currency: str,
    import_key: str,
) -> DataImportResult:
    """Store parsed bars as a single idempotent import."""
    if not bars:
        raise YahooError("No bars to import.")
    if not currency:
        raise YahooError("Yahoo did not report a currency.")
    now = store.clock.now().astimezone(UTC)
    records: list[DataRecord] = []
    for bar in bars:
        key = f"{SOURCE_ID}/{symbol}/{bar.trading_date}/UNADJUSTED"
        records.append(
            DataRecord.create(
                kind=RecordKind.EOD_BAR,
                record_key=key,
                source_id=SOURCE_ID,
                source_url=f"{SOURCE_URL}/quote/{quote(symbol)}",
                unit=f"{currency}/share",
                event_at=bar.event_at,
                known_at=now,
                received_at=now,
                payload={
                    "instrument_id": symbol,
                    "trading_date": bar.trading_date,
                    "adjustment": "UNADJUSTED",
                    "open": str(bar.open),
                    "high": str(bar.high),
                    "low": str(bar.low),
                    "close": str(bar.close),
                    "volume": bar.volume,
                },
            )
        )
    request = {
        "format": "yahoo-chart-v1",
        "symbol": symbol,
        "currency": currency,
        "bars": [bar.trading_date for bar in bars],
    }
    request_hash = sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return store._record_import(
        import_key,
        request_hash,
        tuple(records),
        source_id=SOURCE_ID,
        review_url=SOURCE_URL,
        reviewed_at=now,
    )


class YahooDataClient:
    """Fetch + store EOD bars for a symbol in one call."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Clock | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.transport = transport

    def download(
        self,
        symbol: str,
        *,
        range_: str = "1mo",
        interval: str = "1d",
        import_key: str | None = None,
    ) -> YahooImportResult:
        checked = _validate_symbol(symbol)
        raw = fetch_chart(
            checked, range_=range_, interval=interval, transport=self.transport
        )
        received_at = self.clock.now().astimezone(UTC)
        resolved_symbol, currency, bars = parse_chart(raw, received_at=received_at)
        key = import_key or f"yahoo.{resolved_symbol}.{range_}.{bars[-1].trading_date}"
        from bq.data.records import DataRecordStore as _Store

        store = _Store(self.path, self.clock)
        result = import_bars(
            store, symbol=resolved_symbol, bars=bars, currency=currency, import_key=key
        )
        return YahooImportResult(
            import_id=result.import_id,
            record_count=len(result.record_ids),
            symbol=resolved_symbol,
            records=(),
        )
