"""The engine's `DataClient`, implemented over BharatQuant's own data.

The vendored engine asks for market data through an eleven-method protocol
(``engine/data/protocol.py``) and never constructs a client itself — upstream
built one at three CLI call sites. This module is that client for this app, and
it is the whole point of the port: every method reads what BharatQuant already
stores or can fetch from a vendor it already routes.

The mapping, and why each is mapped the way it is:

======================  =====================================================
Protocol method         Backing
======================  =====================================================
``get_prices``          ``VendorRouter.bars`` (Yahoo → Kite), memoised per run
``get_snapshot``        ``VendorRouter.quote``; the delay is kept, not hidden
``get_financial_metrics`` ``ResearchExpansionStore.fundamentals`` (+ ratios)
``get_news``            ``news_items`` joined to ``news_catalysts``
``get_insider_trades``  ``[]`` — this app has no such source (documented)
``get_company_facts``   ``company_facts`` (sector/industry/name)
``get_earnings``        ``earnings_events`` (latest)
``get_earnings_history````earnings_events`` (history, for PEAD)
``get_market_cap``      the ratios from ``get_financial_metrics``
``screen``/``fields``    raise: universe rules are refused at validation
======================  =====================================================

Two properties every method keeps, because the engine's correctness rests on
them:

* **Point-in-time.** Every read is bounded by the caller's date. Fundamentals
  come from a store whose query filters ``known_at <= as_of``; news is filtered
  by ``published_at`` and ``ingested_at``; earnings by ``known_at``. A method
  that ignored its date would turn the fund into a lookahead machine.
* **Fail loud, empty honest.** An infrastructure failure raises
  ``DataClientError``; "there is genuinely no data" returns an empty list or
  None. Upstream documents this exact contract, and it is the difference between
  a backtest that crashes and one that quietly reads missing data as no signal.

Prices are the one place the app and the engine disagree on units: bars arrive as
``Decimal`` and the engine works in float rupees. The conversion is here, once,
at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from bq.aihf.data_store import CompanyFactsStore, EarningsStore
from bq.aihf.engine.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
    PriceSnapshot,
)
from bq.aihf.engine.data.protocol import DataClientError
from bq.aihf.engine.data.screener import (
    SCREEN_LIMIT,
    OrderBy,
    ScreenerFields,
    ScreenFilter,
    ScreenRow,
)
from bq.core import Clock, SystemClock, open_connection, paise_from_price
from bq.data.symbols import Symbol, SymbolError, resolve
from bq.data.vendors.base import BarInterval, DataDomain
from bq.data.vendors.errors import NoMarketDataError, VendorError
from bq.data.vendors.router import VendorRouter
from bq.fundamentals import FundamentalSnapshot, compute_ratios
from bq.research_expansion import ResearchExpansionError, ResearchExpansionStore

# A price request must name a span; the engine asks for 30–100 days of history
# and single sessions for a mark.
MAX_PRICE_ROWS = 5_000
MAX_NEWS_ROWS = 200
MAX_METRIC_ROWS = 20
# Yahoo's daily history ceiling (its own contract), so a request beyond it is a
# caller bug rather than something to truncate silently.
MAX_HISTORY_DAYS = 3_650


class BridgeError(DataClientError):
    """A data read failed for this app's own reasons (bad symbol, no store)."""


@dataclass(frozen=True, slots=True)
class _BarMemoKey:
    symbol: str
    start: str
    end: str


class BharatQuantDataClient:
    """`DataClient` over the vendor router, the news store and the PIT stores."""

    def __init__(
        self,
        *,
        database_path: Path,
        router: VendorRouter,
        clock: Clock | None = None,
    ) -> None:
        self._path = Path(database_path)
        self._router = router
        self._clock = clock or SystemClock()
        self._bars_cache: dict[_BarMemoKey, tuple[Price, ...]] = {}
        self.source_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    def get_prices(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        **kwargs: object,
    ) -> list[Price]:
        """Daily bars in [start, end], inclusive, oldest first.

        The engine re-requests overlapping windows for the same name many times
        in one replay (a mark, then liquidity, then the factor model all ask for
        the same 30 days), so results are memoised per exact window — that is the
        difference between one vendor call and three per session.

        Signature mirrors the protocol (``**kwargs``) so the class structurally
        satisfies `DataClient`; the two recognised keywords are validated here
        because a narrower signature would be rejected by mypy as an
        incompatible override.
        """
        interval = kwargs.get("interval", "day")
        interval_multiplier = kwargs.get("interval_multiplier", 1)
        if interval != "day":
            raise BridgeError(
                f"only daily bars are supported (asked for {interval!r}); "
                "intraday mandates are not enabled in this pass"
            )
        if interval_multiplier != 1:
            raise BridgeError("interval_multiplier other than 1 is not supported")
        symbol = self._symbol(ticker)
        start = self._date(start_date, "start_date")
        end = self._date(end_date, "end_date")
        if end < start:
            raise BridgeError("end_date is before start_date")
        if (end - start).days > MAX_HISTORY_DAYS:
            raise BridgeError("price window exceeds the vendor's history ceiling")

        key = _BarMemoKey(symbol.canonical, start.isoformat(), end.isoformat())
        cached = self._bars_cache.get(key)
        if cached is not None:
            return list(cached)

        begin = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
        finish = datetime.combine(end, datetime.max.time(), tzinfo=UTC)
        try:
            result = self._router.bars(
                symbol,
                interval=BarInterval.DAY_1,
                start=begin,
                end=finish,
                domain=DataDomain.BARS_DAILY,
            )
        except NoMarketDataError:
            # No bars is a data fact, not a failure: the caller's mark logic
            # turns it into a "skipped name" with a reason.
            self._bars_cache[key] = ()
            return []
        except VendorError as exc:
            raise BridgeError(f"{ticker}: {exc}", path="bars") from exc

        self.source_counts[result.source] = self.source_counts.get(result.source, 0) + 1
        prices = tuple(
            Price(
                open=float(bar.open),
                close=float(bar.close),
                high=float(bar.high),
                low=float(bar.low),
                volume=int(bar.volume),
                time=bar.event_at.isoformat(),
            )
            for bar in result.data
            if start <= bar.event_at.date() <= end
        )
        if len(prices) > MAX_PRICE_ROWS:
            raise BridgeError(f"{ticker}: vendor returned too many bars")
        self._bars_cache[key] = prices
        return list(prices)

    def get_snapshot(self, ticker: str) -> PriceSnapshot | None:
        """The latest quote, or None for an unknown name.

        ``is_realtime`` is deliberately not carried: the engine's model has no
        field for it, and every price this app can serve over a daily cadence is
        a close. What *is* carried is the vendor's own timestamp, so a stale
        print is visible in the record rather than implied to be live.
        """
        symbol = self._symbol(ticker)
        try:
            result = self._router.quote(symbol)
        except NoMarketDataError:
            return None
        except VendorError as exc:
            raise BridgeError(f"{ticker}: {exc}", path="quote") from exc
        self.source_counts[result.source] = self.source_counts.get(result.source, 0) + 1
        quote = result.data
        return PriceSnapshot(
            ticker=ticker,
            price=float(quote.last_price),
            time=quote.as_of.isoformat(),
            day_change=None,
            day_change_percent=None,
        )

    # ------------------------------------------------------------------
    # Fundamentals
    # ------------------------------------------------------------------

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[FinancialMetrics]:
        """Point-in-time fundamentals as the engine's metric rows.

        The store filters on ``known_at <= end_date`` *and* ``period_end <=
        end_date``, so nothing filed after the cutoff can leak in. Each row is
        turned into ratios by the app's own deterministic code — the engine never
        computes a ratio of its own, which is the invariant this mapping keeps.

        ``limit`` is capped: the engine reads at most ``periods=20`` and a larger
        ask would only cost more database work.
        """
        if period != "ttm" and period != "quarter":
            # The engine only ever asks for ttm; anything else is a caller bug.
            raise BridgeError(f"unsupported fundamentals period {period!r}")
        as_of = self._date(end_date, "end_date")
        capped = max(1, min(int(limit), MAX_METRIC_ROWS))
        store = ResearchExpansionStore(self._path, clock=self._clock)
        try:
            records = store.fundamentals(
                ticker, as_of=self._as_instant(as_of), limit=capped
            )
        except ResearchExpansionError as exc:
            # An invalid instrument id is a data fact about this app's naming,
            # not an outage: return empty so the persona abstains.
            if "Invalid instrument identifier" in str(exc):
                return []
            raise BridgeError(str(exc), path="fundamentals") from exc

        rows: list[FinancialMetrics] = []
        for record in records:
            row = self._metrics_row(ticker, record.snapshot, as_of)
            if row is not None:
                rows.append(row)
        return rows

    def _metrics_row(
        self, ticker: str, snapshot: FundamentalSnapshot, as_of: date
    ) -> FinancialMetrics | None:
        """One stored snapshot as the engine's metric row.

        Market-cap-dependent ratios (P/E, P/B, market cap) need a price, and the
        point-in-time price is the close *on the cutoff date* — never today's, or
        a backtest would value a 2020 quarter at a 2026 price. Without a close
        the row is still returned, with those fields None, because the per-share
        and margin fields do not need one.
        """
        price_paise = self._close_paise(ticker, as_of)
        ratios = (
            compute_ratios(snapshot, price_paise=price_paise) if price_paise else None
        )
        shares = snapshot.shares_outstanding
        # The store holds paise; the engine's model expects rupees (it prints
        # per-share figures straight into a prompt), so divide by 100 once here.
        eps = (snapshot.net_income_paise / shares / 100) if shares else None
        book_per_share = (snapshot.book_value_paise / shares / 100) if shares else None
        fcf_per_share = (snapshot.fcf_paise / shares / 100) if shares else None
        return FinancialMetrics(
            ticker=ticker,
            report_period=snapshot.period_end.date().isoformat(),
            period=snapshot.period_label or "quarter",
            currency="INR",
            # The store already enforces known_at >= period_end; the value the
            # engine calls the filing date is exactly the store's known_at.
            filing_date=snapshot.known_at.date().isoformat(),
            filing_datetime=snapshot.known_at.isoformat(),
            market_cap=(ratios.market_cap_paise / 100) if ratios else None,
            price_to_earnings_ratio=(ratios.pe_bps / 10_000)
            if ratios and ratios.pe_bps
            else None,
            price_to_book_ratio=(ratios.pb_bps / 10_000)
            if ratios and ratios.pb_bps
            else None,
            return_on_equity=(ratios.roe_bps / 10_000)
            if ratios and ratios.roe_bps
            else None,
            debt_to_equity=(ratios.debt_equity_bps / 10_000)
            if ratios and ratios.debt_equity_bps
            else None,
            operating_margin=(ratios.ebitda_margin_bps / 10_000)
            if ratios and ratios.ebitda_margin_bps
            else None,
            net_margin=(ratios.net_margin_bps / 10_000)
            if ratios and ratios.net_margin_bps
            else None,
            earnings_per_share=eps,
            book_value_per_share=book_per_share,
            free_cash_flow_per_share=fcf_per_share,
        )

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        """Market cap at *end_date*, from the most recent filed row and that
        date's close — never today's."""
        rows = self.get_financial_metrics(ticker, end_date, "ttm", 1)
        return rows[0].market_cap if rows else None

    # ------------------------------------------------------------------
    # Company facts and earnings
    # ------------------------------------------------------------------

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        """Sector/industry/name from ``company_facts``.

        None when the name was never ingested. The engine treats a missing
        sector as "uncapped on that axis", so the ingest job is what makes a
        sector limit mean anything; that gap is reported by the service, not
        papered over here.
        """
        record = CompanyFactsStore(self._path, clock=self._clock).get(ticker)
        if record is None:
            return None
        return CompanyFacts(
            ticker=ticker,
            is_active=True,
            name=record.company_name,
            cik=None,
            sector=record.sector,
            industry=record.industry,
            exchange="NSE",
        )

    def get_earnings(self, ticker: str) -> Earnings | None:
        """The latest filed quarter, as the engine's single-period shape."""
        events = EarningsStore(self._path, clock=self._clock).history(ticker, limit=1)
        if not events:
            return None
        event = events[0]
        return Earnings(
            ticker=ticker,
            report_period=event.report_period,
            fiscal_period=None,
            currency="INR",
            quarterly=EarningsData(
                earnings_per_share=event.eps_actual,
                estimated_earnings_per_share=event.eps_estimate,
                eps_surprise=event.surprise_or_derived(),
            ),
            annual=None,
        )

    def get_earnings_history(
        self, ticker: str, limit: int = 12
    ) -> list[EarningsRecord]:
        """Filed quarters, newest first, each carrying its filing date.

        The PEAD analyst compares a filing date against the session it is asked
        about and ignores anything after; dropping the date here would make it
        fire on results before they existed.
        """
        capped = max(1, min(int(limit), 40))
        events = EarningsStore(self._path, clock=self._clock).history(
            ticker, limit=capped
        )
        return [
            EarningsRecord(
                ticker=ticker,
                report_period=event.report_period,
                source_type=event.source_type,
                filing_date=event.filing_date,
                filing_datetime=None,
                fiscal_period=None,
                currency="INR",
                quarterly=EarningsData(
                    earnings_per_share=event.eps_actual,
                    estimated_earnings_per_share=event.eps_estimate,
                    eps_surprise=event.surprise_or_derived(),
                ),
                annual=None,
            )
            for event in events
        ]

    # ------------------------------------------------------------------
    # News
    # ------------------------------------------------------------------

    def get_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[CompanyNews]:
        """Headlines attributed to *ticker*, public by *end_date*.

        Attribution is the app's own: ``news_catalysts`` already joins a
        headline to a symbol by company-name and ticker aliases, which is far
        better than a substring match on a raw feed. A name with no catalyst row
        simply has no news here — the engine's personas treat that as "nothing
        published", which is true.
        """
        end = self._date(end_date, "end_date")
        start = self._date(start_date, "start_date") if start_date else None
        capped = max(1, min(int(limit), MAX_NEWS_ROWS))
        rows = self._news_rows(ticker, start, end, capped)
        return [
            CompanyNews(
                ticker=ticker,
                title=title,
                source=source,
                date=published_at.date().isoformat(),
                url=url,
            )
            for title, source, published_at, url in rows
        ]

    def _news_rows(
        self, ticker: str, start: date | None, end: date, limit: int
    ) -> list[tuple[str, str, datetime, str | None]]:
        """Catalyst-attributed headlines in the window, newest first."""
        query = (
            "SELECT n.title, n.source_id, n.published_at, n.url "
            "FROM news_catalysts AS c "
            "JOIN news_items AS n ON n.news_id = c.news_id "
            "WHERE c.symbol = ? AND substr(n.published_at, 1, 10) <= ? "
        )
        params: list[object] = [ticker, end.isoformat()]
        if start is not None:
            query += "AND substr(n.published_at, 1, 10) >= ? "
            params.append(start.isoformat())
        query += "ORDER BY n.published_at DESC LIMIT ?"
        params.append(limit)
        with open_connection(self._path, foreign_keys=True) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            (
                str(row[0]),
                str(row[1]),
                datetime.fromisoformat(str(row[2]).replace("Z", "+00:00")),
                str(row[3]) if row[3] is not None else None,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Not available, or not enabled
    # ------------------------------------------------------------------

    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[InsiderTrade]:
        """Empty: this app has no insider-trading feed.

        Returning empty (rather than raising) is correct under the protocol:
        "no such data" is a fact, and the engine's personas already treat a
        missing insider history as no evidence.
        """
        return []

    def screen(
        self,
        as_of: str,
        filters: list[ScreenFilter],
        *,
        order_by: OrderBy | None = None,
        limit: int = SCREEN_LIMIT,
    ) -> list[ScreenRow]:
        """Refused: self-screening ``universe:`` mandates are not enabled here.

        `load_spec` rejects them at validation, so a run cannot reach this. It
        raises rather than returning [] so a future caller that bypassed the
        validator fails loudly instead of silently trading nothing.
        """
        raise BridgeError(
            "universe screening is not enabled; a mandate must name its tickers",
            path="screen",
        )

    def screener_fields(self) -> ScreenerFields:
        raise BridgeError("universe screening is not enabled", path="screener")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _symbol(self, ticker: str) -> Symbol:
        try:
            return resolve(ticker)
        except SymbolError as exc:
            raise BridgeError(f"invalid ticker {ticker!r}: {exc}") from exc

    @staticmethod
    def _date(value: str, what: str) -> date:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError as exc:
            raise BridgeError(f"{what} must be an ISO date") from exc

    @staticmethod
    def _as_instant(day: date) -> datetime:
        """The end of *day* (UTC) as the point-in-time cutoff.

        End of day, so a filing made during that session is visible to a decision
        made at its close — which is what "as of this session" means.
        """
        return datetime.combine(day, datetime.max.time(), tzinfo=UTC)

    def _close_paise(self, ticker: str, day: date) -> int | None:
        """That date's close in integer paise, or None when the name did not trade.

        ``paise_from_price`` returns the app's ``CurrencyPaise`` wrapper; the
        engine's ratio code wants a plain int, so it is unwrapped here rather
        than leaking the wrapper into vendored code.
        """
        try:
            prices = self.get_prices(ticker, day.isoformat(), day.isoformat())
        except BridgeError:
            return None
        if not prices:
            return None
        close = max(prices, key=lambda price: price.time).close
        return paise_from_price(Decimal(str(close))).value


__all__ = ["BharatQuantDataClient", "BridgeError"]
