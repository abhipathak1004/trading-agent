"""Provider protocols. Every data source sits behind one of these."""

from __future__ import annotations

from datetime import date
from typing import Protocol

import polars as pl


class DailyBarProvider(Protocol):
    """EOD bars for one trading day, all EQ symbols."""

    def fetch(self, trading_day: date) -> pl.DataFrame: ...

    def available(self, trading_day: date) -> bool: ...


class IntradayBarProvider(Protocol):
    """Intraday bars for a symbol on one trading day."""

    def fetch(self, symbol: str, trading_day: date, interval: str = "1m") -> pl.DataFrame: ...
