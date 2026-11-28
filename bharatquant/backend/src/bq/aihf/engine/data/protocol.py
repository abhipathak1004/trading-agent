"""Data provider protocol — the interface all data sources implement.

Any class with these methods can be used as a data provider throughout
the v2 pipeline. No inheritance required — Python's structural typing
handles the rest.

Example::

    class YFinanceClient:
        def get_prices(self, ticker, start_date, end_date, **kwargs):
            # fetch from yfinance, return list[Price]
            ...

    # Works anywhere the pipeline expects a DataClient
    client: DataClient = YFinanceClient()
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bq.aihf.engine.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
    PriceSnapshot,
)
from bq.aihf.engine.data.screener import (
    SCREEN_LIMIT,
    OrderBy,
    ScreenerFields,
    ScreenFilter,
    ScreenRow,
)


class DataClientError(RuntimeError):
    """An API request failed for infrastructure reasons (auth, rate limit,
    server error, network). Distinct from "no data exists" — that returns
    empty. A backtest must crash on this, not treat it as no-data.

    Renamed from the vendored `FDClientError` because this port's client is
    BharatQuant's vendor router, not the Financial Datasets API; the contract
    the agent layer relies on is identical.
    """

    def __init__(self, message: str, *, status_code: int | None = None, path: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path


@runtime_checkable
class DataClient(Protocol):
    """Protocol that all data providers must satisfy.

    Contract: empty list / None means the data genuinely doesn't exist.
    Infrastructure failures (auth, rate limits, network, server errors)
    must RAISE — a provider that silently returns empty on failure poisons
    backtests, because missing data is indistinguishable from "no signal".

    get_financial_metrics must be point-in-time: return only data that was
    publicly filed by *end_date*, not data whose fiscal period ended by then.
    screen must be too: *as_of* is required, and the answer uses only filings
    public on or before it.
    """

    def get_prices(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        **kwargs,
    ) -> list[Price]: ...

    def get_snapshot(self, ticker: str) -> PriceSnapshot | None: ...

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[FinancialMetrics]: ...

    def get_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[CompanyNews]: ...

    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[InsiderTrade]: ...

    def get_company_facts(self, ticker: str) -> CompanyFacts | None: ...

    def get_earnings(self, ticker: str) -> Earnings | None: ...

    def get_earnings_history(
        self,
        ticker: str,
        limit: int = 12,
    ) -> list[EarningsRecord]: ...

    def get_market_cap(self, ticker: str, end_date: str) -> float | None: ...

    def screen(
        self,
        as_of: str,
        filters: list[ScreenFilter],
        *,
        order_by: OrderBy | None = None,
        limit: int = SCREEN_LIMIT,
    ) -> list[ScreenRow]: ...

    def screener_fields(self) -> ScreenerFields: ...
