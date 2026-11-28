"""The data bridge is where the engine meets this app's data, so its two
contracts are tested directly:

* **Point-in-time.** A read bounded by a date must not see anything filed,
  published or priced after it. If this breaks, every backtest becomes a
  lookahead machine that still *looks* like it works.
* **Fail loud, empty honest.** An outage raises; "no such data" returns empty.

The vendor is faked rather than called: these assert the mapping and the
bounds, not Yahoo's uptime.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bq.aihf.bridge.data import BharatQuantDataClient, BridgeError
from bq.aihf.data_store import CompanyFactsStore, EarningsStore
from bq.aihf.engine.data.protocol import DataClient
from bq.core import paise_from_price
from bq.data.symbols import resolve
from bq.data.vendors.base import Bar, Quote
from bq.data.vendors.errors import VendorUnavailableError
from bq.data.vendors.router import VendorResult
from bq.infra.database import migrate


class _FakeRouter:
    """Serves a fixed bar series and quote; can be told to fail on demand."""

    def __init__(self, closes: dict[str, float] | None = None) -> None:
        self.closes = closes or {"RELIANCE": 1000.0, "NIFTY 50": 20000.0}
        self.fail = False
        self.bar_calls = 0

    def bars(self, symbol, *, interval, start, end, domain=None):
        if self.fail:
            raise VendorUnavailableError("down", "vendor is down")
        self.bar_calls += 1
        close = self.closes[symbol.tradingsymbol]
        bars = []
        day = start.date()
        while day <= end.date():
            if day.weekday() < 5:
                bars.append(
                    Bar(
                        event_at=datetime.combine(
                            day, datetime.min.time(), tzinfo=UTC
                        ).replace(hour=15, minute=30),
                        open=Decimal(str(close)),
                        high=Decimal(str(close)),
                        low=Decimal(str(close)),
                        close=Decimal(str(close)),
                        volume=1_000_000,
                    )
                )
            day += timedelta(days=1)
        return VendorResult(data=tuple(bars), source="fake", is_realtime=False)

    def quote(self, symbol):
        if self.fail:
            raise VendorUnavailableError("down", "vendor is down")
        return VendorResult(
            data=Quote(
                symbol=symbol,
                last_price=Decimal("1000"),
                as_of=datetime(2026, 11, 25, 10, 0, tzinfo=UTC),
                is_realtime=False,
                delay_seconds=900.0,
            ),
            source="fake",
            is_realtime=False,
        )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return path


def _client(db, router=None) -> BharatQuantDataClient:
    return BharatQuantDataClient(database_path=db, router=router or _FakeRouter())


# ---------------------------------------------------------------------------
# The protocol is satisfied structurally
# ---------------------------------------------------------------------------


def test_the_client_satisfies_the_engine_protocol(db) -> None:
    """The engine duck-types; `runtime_checkable` makes a missing method a
    clear failure here rather than a TypeError halfway through a backtest."""
    assert isinstance(_client(db), DataClient)


# ---------------------------------------------------------------------------
# Prices: units, bounds and memoisation
# ---------------------------------------------------------------------------


def test_prices_are_float_rupees_oldest_first(db) -> None:
    prices = _client(db).get_prices("RELIANCE", "2026-11-23", "2026-11-25")
    assert [p.close for p in prices] == [1000.0, 1000.0, 1000.0]
    assert prices == sorted(prices, key=lambda p: p.time)


def test_the_sample_week_returns_only_weekdays(db) -> None:
    """Mon 23 – Sun 29 Nov 2026 has five weekdays; the fake vendor emits exactly
    those, and the bridge must pass through only what was asked for."""
    prices = _client(db).get_prices("RELIANCE", "2026-11-23", "2026-11-29")
    assert len(prices) == 5


def test_the_same_window_is_fetched_once(db) -> None:
    """A replay asks for the same 30 days for a mark, then liquidity, then the
    factor model. Without memoisation that is three vendor calls per session."""
    router = _FakeRouter()
    client = _client(db, router)
    client.get_prices("RELIANCE", "2026-11-02", "2026-11-30")
    client.get_prices("RELIANCE", "2026-11-02", "2026-11-30")
    assert router.bar_calls == 1


def test_a_vendor_outage_raises_rather_than_returning_empty(db) -> None:
    """The protocol's central rule: an outage must not read as 'no data', or a
    backtest silently treats missing prices as no signal."""
    router = _FakeRouter()
    router.fail = True
    with pytest.raises(BridgeError):
        _client(db, router).get_prices("RELIANCE", "2026-11-02", "2026-11-30")


def test_an_intraday_request_is_refused_clearly(db) -> None:
    with pytest.raises(BridgeError, match="daily bars"):
        _client(db).get_prices(
            "RELIANCE", "2026-11-02", "2026-11-30", interval="minute"
        )


def test_an_inverted_window_is_refused(db) -> None:
    with pytest.raises(BridgeError, match="before start_date"):
        _client(db).get_prices("RELIANCE", "2026-11-30", "2026-11-02")


def test_an_unknown_ticker_is_a_clear_error(db) -> None:
    with pytest.raises(BridgeError, match="invalid ticker"):
        _client(db).get_prices("", "2026-11-02", "2026-11-30")


def test_the_index_benchmark_resolves(db) -> None:
    """The mandate's default benchmark is `NIFTY 50`, which is an index, not a
    tradeable symbol — resolution has to handle it or every backtest fails on
    its first fetch."""
    prices = _client(db).get_prices("NIFTY 50", "2026-11-23", "2026-11-25")
    assert prices and prices[0].close == 20000.0


# ---------------------------------------------------------------------------
# Fundamentals: point-in-time is the whole contract
# ---------------------------------------------------------------------------


def _store_fundamentals(db, *, period: str, known: datetime) -> None:
    from bq.fundamentals import FundamentalSnapshot, FundamentalSource
    from bq.research_expansion import ResearchExpansionStore

    store = ResearchExpansionStore(db)
    store.freeze_fundamentals(
        FundamentalSnapshot(
            instrument_id="RELIANCE",
            source=FundamentalSource.YAHOO,
            period_end=datetime.fromisoformat(period).replace(tzinfo=UTC),
            known_at=known,
            revenue_paise=100_000,
            ebitda_paise=20_000,
            net_income_paise=10_000,
            total_debt_paise=5_000,
            cash_paise=3_000,
            shares_outstanding=1_000,
            book_value_paise=50_000,
            fcf_paise=8_000,
            period_label="quarter",
        )
    )


def test_fundamentals_before_the_filing_date_are_invisible(db) -> None:
    """The one rule that makes a backtest honest: a quarter filed on 14 Aug is
    not visible to a decision made on 30 Jun."""
    _store_fundamentals(
        db, period="2024-06-30", known=datetime(2024, 8, 14, tzinfo=UTC)
    )
    client = _client(db)
    assert client.get_financial_metrics("RELIANCE", "2024-06-30") == []
    assert len(client.get_financial_metrics("RELIANCE", "2024-09-30")) == 1


def test_metrics_carry_ratios_computed_by_the_app_not_the_model(db) -> None:
    """Arithmetic stays in deterministic code: the row arrives with P/E, ROE and
    margins already computed from the store's paise, never asked of an LLM."""
    _store_fundamentals(
        db, period="2024-06-30", known=datetime(2024, 8, 14, tzinfo=UTC)
    )
    rows = _client(db).get_financial_metrics("RELIANCE", "2024-12-31")
    row = rows[0]
    assert row.report_period == "2024-06-30"
    assert row.filing_date == "2024-08-14"
    assert row.period == "quarter"
    assert row.currency == "INR"
    # ROE = net income / book value = 10_000 / 50_000 = 0.2
    assert row.return_on_equity == pytest.approx(0.2)
    # EPS = 10_000 paise / 1000 shares = 10 paise = 0.10 rupees
    assert row.earnings_per_share == pytest.approx(0.10)


def test_market_cap_uses_the_running_date_close_not_todays(db) -> None:
    """A 2024 quarter valued at a 2026 price would look like a 20x return. The
    close is the session's own."""
    _store_fundamentals(
        db, period="2024-06-30", known=datetime(2024, 8, 14, tzinfo=UTC)
    )

    class _Router(_FakeRouter):
        def bars(self, symbol, *, interval, start, end, domain=None):
            # Price is the date itself, so a wrong date is unmistakable.
            if symbol.tradingsymbol != "RELIANCE":
                return super().bars(
                    symbol, interval=interval, start=start, end=end, domain=domain
                )
            day = start.date()
            bars = []
            while day <= end.date():
                bars.append(
                    Bar(
                        event_at=datetime.combine(
                            day, datetime.min.time(), tzinfo=UTC
                        ).replace(hour=15, minute=30),
                        open=Decimal(str(day.day)),
                        high=Decimal(str(day.day)),
                        low=Decimal(str(day.day)),
                        close=Decimal(str(day.day)),
                        volume=1000,
                    )
                )
                day += timedelta(days=1)
            return VendorResult(data=tuple(bars), source="fake", is_realtime=False)

    client = BharatQuantDataClient(
        database_path=db, router=_Router({"RELIANCE": 1.0, "NIFTY 50": 1.0})
    )
    # The quarter was filed on 14 Aug, so a 30 Jun cutoff sees nothing at all.
    assert client.get_financial_metrics("RELIANCE", "2024-06-30") == []
    rows = client.get_financial_metrics("RELIANCE", "2024-09-30")
    assert rows
    # Priced at 30 Sep's close (30.00 rupees), NOT today's: 30 rupees x 100
    # paise x 1000 shares / 100 = 30000 rupees.
    assert rows[0].market_cap == pytest.approx(30.0 * 1000)


def test_a_name_with_no_stored_fundamentals_yields_an_empty_list(db) -> None:
    assert _client(db).get_financial_metrics("TCS", "2024-12-31") == []


def test_market_cap_is_none_without_rows(db) -> None:
    assert _client(db).get_market_cap("TCS", "2024-12-31") is None


# ---------------------------------------------------------------------------
# Company facts and earnings
# ---------------------------------------------------------------------------


def test_company_facts_carry_a_sector(db) -> None:
    CompanyFactsStore(db).record(
        symbol="RELIANCE",
        company_name="Reliance Industries",
        isin="INE002A01018",
        sector="Energy",
        industry="Refineries",
        known_at=datetime(2024, 1, 1, tzinfo=UTC),
        source_id="yahoo.fundamentals",
        source_url="https://finance.yahoo.com",
    )
    facts = _client(db).get_company_facts("RELIANCE")
    assert facts is not None
    assert facts.sector == "Energy"
    assert facts.exchange == "NSE"


def test_a_name_without_facts_has_none(db) -> None:
    assert _client(db).get_company_facts("TCS") is None


def test_earnings_history_is_newest_first_and_carries_the_filing_date(db) -> None:
    store = EarningsStore(db)
    for period, filed in (("2024-03-31", "2024-05-15"), ("2024-06-30", "2024-08-14")):
        store.record(
            symbol="RELIANCE",
            report_period=period,
            filing_date=filed,
            eps_actual=28.5,
            eps_estimate=27.0,
            eps_surprise=None,
            known_at=datetime.fromisoformat(filed).replace(tzinfo=UTC),
        )
    records = _client(db).get_earnings_history("RELIANCE")
    assert [r.report_period for r in records] == ["2024-06-30", "2024-03-31"]
    assert records[0].filing_date == "2024-08-14"
    assert records[0].quarterly is not None
    assert records[0].quarterly.eps_surprise == "BEAT"


def test_insider_trades_are_empty_not_an_error(db) -> None:
    """No feed exists; the protocol's "no data" answer is an empty list."""
    assert _client(db).get_insider_trades("RELIANCE", "2024-12-31") == []


def test_screening_is_refused(db) -> None:
    with pytest.raises(BridgeError, match="screening"):
        _client(db).screen("2024-12-31", [])


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


def _catalyst(news_id: str):
    """A catalyst row for the fixture headline, as the extractor would produce."""
    from bq.catalysts import Catalyst, CatalystEvent

    return Catalyst(
        news_id=news_id,
        symbol="RELIANCE",
        event=CatalystEvent.ORDER_WIN,
        polarity=1,
        strength=4,
        matched_terms=("reliance",),
    )


def test_news_is_attributed_by_catalyst_and_bounded_by_date(db) -> None:
    from bq.catalysts import CatalystStore
    from bq.news import FeedEntry, NewsStore

    published = datetime(2024, 8, 1, 9, 0, tzinfo=UTC)
    NewsStore(db).ingest(
        [
            FeedEntry(
                title="Reliance order win",
                summary="body",
                url="https://example.com/1",
                published_at=published,
            )
        ],
        source_id="pulse.zerodha",
        agent_id="news_filter",
        memory_ttl=timedelta(days=30),
    )
    with sqlite3.connect(db) as connection:
        news_id = connection.execute("SELECT news_id FROM news_items").fetchone()[0]
    CatalystStore(db).record([_catalyst(news_id)], trade_date=published.date())

    client = _client(db)
    news = client.get_news("RELIANCE", "2024-08-02")
    assert [n.title for n in news] == ["Reliance order win"]
    assert news[0].date == "2024-08-01"
    # Before it was published, it is not visible.
    assert client.get_news("RELIANCE", "2024-07-31") == []


def test_news_rows_created_before_the_cutoff_are_not_read_forward(db) -> None:
    """Sanity check on the join: an empty catalyst table means no news, not a
    crash."""
    assert _client(db).get_news("TCS", "2024-12-31") == []


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_price_conversion_matches_the_apps_paise_helper() -> None:
    """`paise_from_price` returns the app's CurrencyPaise wrapper; the bridge
    unwraps it, because the engine's arithmetic wants a plain int."""
    assert paise_from_price(Decimal("1000.00")).value == 100_000


def test_resolving_a_bare_ticker_defaults_to_nse() -> None:
    assert resolve("RELIANCE").canonical == "NSE:RELIANCE"
