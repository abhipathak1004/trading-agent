"""The market-data vendor contract.

Two vendor-shaped things already exist in this package and this is a third,
deliberately:

* ``bq.data.providers.DataProvider`` — exact-key, completeness-required point-in
  time reads. Wrong shape for "give me 200 bars": a partial answer is normal
  there and the caller wants the freshest vendor that can answer.
* ``bq.data.yahoo`` — one concrete source, with its own parsing and validation.
* **This module** — the small contract a router can fan a request across many
  sources with, carrying provenance back with the data.

Every vendor declares which ``DataDomain`` values it can serve, and whether what
it returns is real-time. That flag travels with the result so nothing
downstream has to remember which vendor produced a price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from bq.data.symbols import Symbol


class DataDomain(StrEnum):
    """What kind of thing is being asked for.

    Routing is per-domain, because the right primary source differs: Kite is
    best for live quotes and intraday candles, Yahoo for long daily history.
    """

    QUOTES_LIVE = "quotes.live"
    BARS_INTRADAY = "bars.intraday"
    BARS_DAILY = "bars.daily"
    INDICES = "indices"


class BarInterval(StrEnum):
    """The candle widths this application actually uses."""

    MINUTE_1 = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    HOUR_1 = "60m"
    DAY_1 = "1d"


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV candle.

    ``event_at`` is the instant the bar belongs to (the bar's own timestamp),
    never the fetch time. Prices are ``Decimal`` here because they came off the
    wire as exact strings; pandas converts to float64 only inside the indicator
    module.
    """

    event_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        if self.event_at.utcoffset() is None:
            raise ValueError("A bar timestamp must be timezone-aware.")
        for value in (self.open, self.high, self.low, self.close):
            if not isinstance(value, Decimal) or value <= 0:
                raise ValueError("A bar price must be a positive Decimal.")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int):
            raise ValueError("A bar volume must be an integer.")
        if self.volume < 0:
            raise ValueError("A bar volume must not be negative.")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("A bar high is below another price.")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("A bar low is above another price.")


@dataclass(frozen=True, slots=True)
class Quote:
    """A latest-price snapshot.

    ``as_of`` is when the price was true; ``delay_seconds`` says how stale that
    is. A delayed Yahoo quote is legitimate for research and must never be
    treated as real-time for an order (which is why ``is_realtime`` exists).
    """

    symbol: Symbol
    last_price: Decimal
    as_of: datetime
    is_realtime: bool
    delay_seconds: float
    day_open: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    day_close: Decimal | None = None
    volume: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol):
            raise ValueError("A quote requires a resolved symbol.")
        if not isinstance(self.last_price, Decimal) or self.last_price <= 0:
            raise ValueError("A quote price must be a positive Decimal.")
        if self.as_of.utcoffset() is None:
            raise ValueError("A quote timestamp must be timezone-aware.")
        if not isinstance(self.is_realtime, bool):
            raise ValueError("is_realtime must be a boolean.")
        if isinstance(self.delay_seconds, bool) or self.delay_seconds < 0:
            raise ValueError("A quote delay must be nonnegative.")


@runtime_checkable
class MarketDataVendor(Protocol):
    """What the router needs from a source.

    Implementations raise :class:`bq.data.vendors.errors.VendorUnavailableError`
    when they cannot serve and :class:`NoMarketDataError` when there is no data,
    and never invent a value to avoid raising.
    """

    @property
    def name(self) -> str: ...

    @property
    def is_realtime(self) -> bool: ...

    @property
    def capabilities(self) -> frozenset[DataDomain]: ...

    def quote(self, symbol: Symbol) -> Quote: ...

    def bars(
        self,
        symbol: Symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> tuple[Bar, ...]: ...


__all__ = [
    "Bar",
    "BarInterval",
    "DataDomain",
    "MarketDataVendor",
    "Quote",
]
