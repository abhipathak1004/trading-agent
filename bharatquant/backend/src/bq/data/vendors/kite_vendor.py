"""Zerodha Kite Connect as a **read-only** market-data vendor.

ADR-013 allows exactly four Kite calls here: ``instruments``, ``quote``,
``ohlc`` and ``historical_data``. There is no order placement, no modification
and no cancellation — and ``tests/test_kite_vendor.py`` asserts that this module
exposes no such method, so a future edit cannot quietly add one.

The official ``kiteconnect`` client is injected, not constructed inside the
vendor, so tests run without credentials or a network and the caller controls
the token lifecycle.

Kite's daily access token expires every morning. An expired or rejected token is
translated to ``VendorUnavailableError("kite_auth_expired")``, which lets the
router fall through to Yahoo for read-only analysis instead of failing the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from bq.data.symbols import Symbol, SymbolError, to_kite
from bq.data.vendors.base import Bar, BarInterval, DataDomain, Quote
from bq.data.vendors.errors import NoMarketDataError, VendorUnavailableError

# Kite limits a single historical request to this many days per interval.
_MAX_SPAN_DAYS = {
    BarInterval.MINUTE_1: 60,
    BarInterval.MINUTE_5: 100,
    BarInterval.MINUTE_15: 200,
    BarInterval.HOUR_1: 400,
    BarInterval.DAY_1: 2000,
}

_KITE_INTERVAL = {
    BarInterval.MINUTE_1: "minute",
    BarInterval.MINUTE_5: "5minute",
    BarInterval.MINUTE_15: "15minute",
    BarInterval.HOUR_1: "60minute",
    BarInterval.DAY_1: "day",
}

_AUTH_ERROR_TYPES = frozenset({"TokenException", "PermissionException"})

# Hard ceiling on chunked requests, so a caller cannot turn one analysis run into
# hundreds of broker calls by asking for a decade of 1-minute candles.
MAX_CHUNKS = 12


class _KiteClient(Protocol):
    """The slice of ``kiteconnect.KiteConnect`` this vendor is allowed to use."""

    def instruments(self, exchange: str | None = ...) -> list[dict[str, object]]: ...

    def quote(self, *instruments: str) -> dict[str, dict[str, object]]: ...

    def ohlc(self, *instruments: str) -> dict[str, dict[str, object]]: ...

    def historical_data(
        self,
        instrument_token: int,
        from_date: datetime | str,
        to_date: datetime | str,
        interval: str,
        continuous: bool = ...,
        oi: bool = ...,
    ) -> list[dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class _TokenSource(Protocol):
    def credentials(self) -> tuple[str, str, str] | None: ...


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if value != value:  # NaN
            return None
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError:
            return None
    return None


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.utcoffset() else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.utcoffset() else parsed.replace(tzinfo=UTC)
    return None


def _is_auth_error(exc: Exception) -> bool:
    return type(exc).__name__ in _AUTH_ERROR_TYPES


class KiteVendor:
    """Real-time quotes and candles from Kite. Read-only by construction."""

    name = "kite"
    is_realtime = True
    capabilities = frozenset(
        {
            DataDomain.QUOTES_LIVE,
            DataDomain.BARS_INTRADAY,
            DataDomain.BARS_DAILY,
            DataDomain.INDICES,
        }
    )

    def __init__(
        self,
        client: _KiteClient,
        *,
        instrument_tokens: dict[tuple[str, str], int] | None = None,
        clock: object | None = None,
    ) -> None:
        self._client = client
        # Injected rather than fetched here: token lookup is the caller's
        # InstrumentMasterStore's job, and injection keeps this testable.
        self._tokens = dict(instrument_tokens or {})
        self._clock = clock

    def _now(self) -> datetime:
        clock = self._clock
        now = getattr(clock, "now", None)
        if callable(now):
            value = now()
            if isinstance(value, datetime) and value.utcoffset() is not None:
                return value.astimezone(UTC)
        return datetime.now(UTC)

    def set_instrument_tokens(self, tokens: dict[tuple[str, str], int]) -> None:
        self._tokens = dict(tokens)

    def _token(self, symbol: Symbol) -> int:
        try:
            exchange, tradingsymbol = to_kite(symbol)
        except SymbolError as exc:
            raise NoMarketDataError(symbol.canonical) from exc
        token = self._tokens.get((exchange, tradingsymbol))
        if token is None:
            raise VendorUnavailableError(
                "instrument_master_missing",
                f"No Kite instrument token for {symbol.canonical}.",
            )
        return token

    # -- quotes -----------------------------------------------------------

    def quote(self, symbol: Symbol) -> Quote:
        try:
            exchange, tradingsymbol = to_kite(symbol)
        except SymbolError as exc:
            raise NoMarketDataError(symbol.canonical) from exc
        # Validate against the instrument master before asking: it proves the
        # ticker actually exists on that exchange, and keeps quote() and bars()
        # consistent about what "we know this instrument" means.
        self._token(symbol)
        key = f"{exchange}:{tradingsymbol}"
        try:
            payload = self._client.quote(key)
        except Exception as exc:
            if _is_auth_error(exc):
                raise VendorUnavailableError("kite_auth_expired", str(exc)) from exc
            raise VendorUnavailableError(
                "transport", f"Kite quote failed: {type(exc).__name__}."
            ) from exc

        entry = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(entry, dict):
            raise NoMarketDataError(symbol.canonical)
        last_price = _decimal(entry.get("last_price"))
        if last_price is None or last_price <= 0:
            raise NoMarketDataError(symbol.canonical)
        timestamp = _as_datetime(entry.get("last_trade_time")) or _as_datetime(
            entry.get("timestamp")
        )
        as_of = timestamp or self._now()
        ohlc = entry.get("ohlc")
        depth = ohlc if isinstance(ohlc, dict) else {}
        volume = entry.get("volume")
        return Quote(
            symbol=symbol,
            last_price=last_price,
            as_of=as_of,
            is_realtime=self.is_realtime,
            delay_seconds=max(0.0, (self._now() - as_of).total_seconds()),
            day_open=_decimal(depth.get("open")),
            day_high=_decimal(depth.get("high")),
            day_low=_decimal(depth.get("low")),
            day_close=_decimal(depth.get("close")),
            volume=int(volume) if isinstance(volume, int) and volume >= 0 else None,
        )

    # -- bars -------------------------------------------------------------

    def bars(
        self,
        symbol: Symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> tuple[Bar, ...]:
        token = self._token(symbol)
        start = start.astimezone(UTC)
        end = min(end.astimezone(UTC), self._now())
        if end <= start:
            raise NoMarketDataError(symbol.canonical)

        kite_interval = _KITE_INTERVAL[interval]
        max_span = timedelta(days=_MAX_SPAN_DAYS[interval])
        if end - start > max_span * MAX_CHUNKS:
            raise ValueError(
                f"A single Kite request must span at most "
                f"{(max_span * MAX_CHUNKS).days} days of {interval.value} data."
            )

        # Kite rejects a request wider than max_span, so walk the requested
        # window forward in chunks. Starting at ``start`` (not at ``end``) is
        # what guarantees the earliest requested bar is never dropped.
        collected: list[Bar] = []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + max_span, end)
            try:
                rows = self._client.historical_data(
                    token, cursor, chunk_end, kite_interval
                )
            except Exception as exc:
                if _is_auth_error(exc):
                    raise VendorUnavailableError("kite_auth_expired", str(exc)) from exc
                raise VendorUnavailableError(
                    "transport", f"Kite history failed: {type(exc).__name__}."
                ) from exc
            collected.extend(self._parse_bars(rows))
            cursor = chunk_end

        bars = tuple(bar for bar in collected if start <= bar.event_at <= end)
        if not bars:
            raise NoMarketDataError(symbol.canonical)
        return tuple(sorted(bars, key=lambda bar: bar.event_at))

    @staticmethod
    def _parse_bars(rows: object) -> list[Bar]:
        if not isinstance(rows, list):
            raise VendorUnavailableError("bad_response", "Kite history was not a list.")
        bars: list[Bar] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            event_at = _as_datetime(row.get("date"))
            open_p = _decimal(row.get("open"))
            high_p = _decimal(row.get("high"))
            low_p = _decimal(row.get("low"))
            close_p = _decimal(row.get("close"))
            volume = row.get("volume")
            if event_at is None or None in (open_p, high_p, low_p, close_p):
                continue
            assert open_p is not None and high_p is not None
            assert low_p is not None and close_p is not None
            if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
                continue
            try:
                bars.append(
                    Bar(
                        event_at=event_at,
                        open=open_p,
                        high=high_p,
                        low=low_p,
                        close=close_p,
                        volume=volume,
                    )
                )
            except ValueError:
                continue
        return bars


__all__ = ["KiteVendor"]
