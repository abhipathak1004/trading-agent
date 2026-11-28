"""Yahoo Finance as a market-data vendor.

This is a thin adapter, not a second Yahoo client. It reuses
``bq.data.yahoo.parse_chart`` — the same bounded, typed parser the rest of the
project already trusts — and adds only what the vendor contract needs:

* an intraday window fetch (``period1``/``period2``), which the existing
  range-based helper does not do;
* mapping to :class:`bq.data.vendors.base.Bar` / ``Quote``;
* ``VendorError`` translation.

Yahoo is unofficial and delayed. ``is_realtime`` is therefore always ``False``,
and every quote reports a ``delay_seconds`` floor so downstream code can tell a
delayed Yahoo price from a live Kite one without guessing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote as url_quote

import httpx

from bq.data.symbols import Symbol, SymbolError, to_yahoo
from bq.data.vendors.base import (
    Bar,
    BarInterval,
    DataDomain,
    Quote,
)
from bq.data.vendors.errors import NoMarketDataError, VendorUnavailableError
from bq.data.yahoo import (
    DEFAULT_TIMEOUT_SECONDS,
    YAHOO_ENDPOINT,
    YahooError,
    parse_chart,
    stream_chart,
)

BASE_DELAY_SECONDS = 900.0  # Yahoo quotes are delayed; 15 minutes is the floor
_USER_AGENT = "Mozilla/5.0 BharatQuant/0.1 (personal research)"

# Yahoo's own intraday lookback limits. Asking for more returns an empty series,
# so refuse up front with a clear reason rather than silently returning nothing.
_INTERVAL_LOOKBACK = {
    BarInterval.MINUTE_1: timedelta(days=7),
    BarInterval.MINUTE_5: timedelta(days=60),
    BarInterval.MINUTE_15: timedelta(days=60),
    BarInterval.HOUR_1: timedelta(days=730),
    BarInterval.DAY_1: timedelta(days=3650),
}

# bq.data.yahoo._VALID_INTERVALS is daily-only by design. Map our richer set.
_YAHOO_INTERVAL = {
    BarInterval.MINUTE_1: "1m",
    BarInterval.MINUTE_5: "5m",
    BarInterval.MINUTE_15: "15m",
    BarInterval.HOUR_1: "60m",
    BarInterval.DAY_1: "1d",
}


def _fetch_window(
    yahoo_symbol: str,
    *,
    interval: str,
    start: datetime,
    end: datetime,
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> bytes:
    """Fetch a bounded chart window with period1/period2."""
    url = YAHOO_ENDPOINT.format(symbol=url_quote(yahoo_symbol))
    params: dict[str, str | int] = {
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
        "interval": interval,
        "includePrePost": "false",
        "events": "div,splits",
    }
    try:
        return stream_chart(
            url,
            params=params,
            timeout=timeout,
            transport=transport,
            too_large=VendorUnavailableError(
                "response_too_large", "Yahoo response exceeded the size limit."
            ),
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 429:
            raise VendorUnavailableError(
                "rate_limited", "Yahoo rate limited the request."
            )
        if status >= 500:
            raise VendorUnavailableError("upstream_error", f"Yahoo returned {status}.")
        raise VendorUnavailableError("http_error", f"Yahoo returned {status}.")
    except httpx.HTTPError as exc:
        raise VendorUnavailableError(
            "transport", f"Yahoo request failed: {type(exc).__name__}."
        )


class YahooVendor:
    """Delayed Yahoo Finance data. Never templates itself as real-time."""

    name = "yahoo"
    is_realtime = False
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
        *,
        clock: object | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._clock = clock
        self.transport = transport
        self.timeout = timeout

    def _received_at(self) -> datetime:
        clock = self._clock
        now = getattr(clock, "now", None)
        if callable(now):
            value = now()
            if isinstance(value, datetime) and value.utcoffset() is not None:
                return value.astimezone(UTC)
        return datetime.now(UTC)

    def _bars(
        self, symbol: Symbol, interval: BarInterval, start: datetime, end: datetime
    ) -> tuple[Bar, ...]:
        try:
            yahoo_symbol = to_yahoo(symbol)
        except SymbolError as exc:
            raise NoMarketDataError(symbol.canonical) from exc

        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        lookback = _INTERVAL_LOOKBACK[interval]
        if end - start > lookback:
            raise ValueError(
                f"Yahoo supports at most {lookback.days} days of {interval.value} data."
            )
        if end > self._received_at():
            end = self._received_at()

        raw = _fetch_window(
            yahoo_symbol,
            interval=_YAHOO_INTERVAL[interval],
            start=start,
            end=end,
            timeout=self.timeout,
            transport=self.transport,
        )
        received_at = self._received_at()
        try:
            _resolved, _currency, yahoo_bars = parse_chart(raw, received_at=received_at)
        except YahooError as exc:
            message = str(exc)
            if "no complete bars" in message or "no chart data" in message:
                raise NoMarketDataError(symbol.canonical) from exc
            raise VendorUnavailableError("bad_response", message) from exc

        bars = tuple(
            Bar(
                event_at=bar.event_at,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in yahoo_bars
            if start <= bar.event_at <= end
        )
        if not bars:
            raise NoMarketDataError(symbol.canonical)
        return bars

    def bars(
        self,
        symbol: Symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> tuple[Bar, ...]:
        return self._bars(symbol, interval, start, end)

    def quote(self, symbol: Symbol) -> Quote:
        # Yahoo has no free real-time quote endpoint here; the most recent
        # completed daily bar is the honest answer, and it is delayed by
        # construction.
        end = self._received_at()
        start = end - _INTERVAL_LOOKBACK[BarInterval.DAY_1]
        bars = self._bars(symbol, BarInterval.DAY_1, start, end)
        latest = bars[-1]
        delay = max(BASE_DELAY_SECONDS, (end - latest.event_at).total_seconds())
        return Quote(
            symbol=symbol,
            last_price=latest.close,
            as_of=latest.event_at,
            is_realtime=False,
            delay_seconds=delay,
            day_open=latest.open,
            day_high=latest.high,
            day_low=latest.low,
            day_close=latest.close,
            volume=latest.volume,
        )


__all__ = ["BASE_DELAY_SECONDS", "YahooVendor"]
