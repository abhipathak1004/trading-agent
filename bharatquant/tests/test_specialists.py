from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from bq.analysis.briefs import SpecialistBrief
from bq.analysis.debate import Debate
from bq.analysis.fundamental import FundamentalEventsAgent
from bq.analysis.news_context import NewsContextAgent
from bq.analysis.pipeline import AnalysisPipeline
from bq.analysis.regime import MarketRegimeAgent, classify_regime
from bq.analysis.technical import SubScore, TechnicalReport
from bq.compute.indicators import IndicatorSnapshot
from bq.data.symbols import Symbol, resolve
from bq.data.vendors import (
    Bar,
    BarInterval,
    DataDomain,
    NoMarketDataError,
    Quote,
    VendorRouter,
)
from bq.infra.database import migrate
from bq.news import FeedEntry, NewsStore

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _news_store(tmp_path: Path) -> NewsStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return NewsStore(path, FrozenClock())


def _ingest(store: NewsStore, *, title: str, published_at: datetime, url: str) -> None:
    store.ingest(
        [
            FeedEntry(
                title=title,
                summary="Synthetic summary.",
                url=url,
                published_at=published_at,
            )
        ]
    )


def test_news_recent_is_point_in_time(tmp_path: Path) -> None:
    store = _news_store(tmp_path)
    _ingest(
        store,
        title="Reliance Industries wins a large order",
        published_at=NOW - timedelta(hours=1),
        url="https://example.com/reliance",
    )
    _ingest(
        store,
        title="TCS announces a dividend",
        published_at=NOW + timedelta(hours=1),
        url="https://example.com/tcs",
    )
    current = store.recent(limit=10, as_of=NOW)
    all_items = store.recent(limit=10)
    assert [item.title for item in current] == [
        "Reliance Industries wins a large order"
    ]
    assert len(all_items) == 2


def test_news_context_matches_the_symbol_and_keeps_provenance(tmp_path: Path) -> None:
    store = _news_store(tmp_path)
    _ingest(
        store,
        title="Reliance Industries wins a large order",
        published_at=NOW - timedelta(hours=1),
        url="https://example.com/reliance",
    )
    _ingest(
        store,
        title="TCS announces a dividend",
        published_at=NOW - timedelta(hours=1),
        url="https://example.com/tcs",
    )
    brief = NewsContextAgent(store).brief(resolve("NSE:RELIANCE"), as_of=NOW)
    assert brief.agent == "news"
    assert brief.source == "news_store"
    assert any("Reliance" in fact for fact in brief.facts)
    assert all("TCS" not in fact for fact in brief.facts)
    assert brief.claims and brief.claims[0].evidence_ids


def test_fundamental_events_extracts_corporate_headlines(tmp_path: Path) -> None:
    store = _news_store(tmp_path)
    _ingest(
        store,
        title="Reliance Industries announces a dividend and record date",
        published_at=NOW - timedelta(hours=1),
        url="https://example.com/reliance-dividend",
    )
    _ingest(
        store,
        title="Reliance Industries opens a new store",
        published_at=NOW - timedelta(hours=1),
        url="https://example.com/reliance-store",
    )
    brief = FundamentalEventsAgent(store).brief(resolve("NSE:RELIANCE"), as_of=NOW)
    assert brief.agent == "fundamental_events"
    assert any("dividend" in fact for fact in brief.facts)
    assert any("Event risk: 1 matching" in fact for fact in brief.facts)


def test_regime_classifier() -> None:
    common = {
        "close": 100.0,
        "sma20": 98.0,
        "sma50": 95.0,
        "return20": 0.04,
        "adx14": 28.0,
        "vix": 12.0,
        "vix_mean": 12.0,
        "atr_pct": 0.01,
    }
    assert classify_regime(**common) == "trend_up"
    assert (
        classify_regime(
            **{
                **common,
                "close": 90.0,
                "sma20": 94.0,
                "sma50": 99.0,
                "return20": -0.04,
            }
        )
        == "trend_down"
    )
    assert classify_regime(**{**common, "vix": 30.0}) == "high_vol_stress"
    assert classify_regime(**{**common, "return20": 0.0, "adx14": 10.0}) == "range"


def _bars(*, base: float, step: float, count: int = 120) -> tuple[Bar, ...]:
    start = NOW - timedelta(days=count)
    out = []
    for index in range(count):
        price = Decimal(str(round(base + index * step, 2)))
        half = Decimal("0.5")
        out.append(
            Bar(
                event_at=start + timedelta(days=index),
                open=price,
                high=price + half,
                low=price - half,
                close=price,
                volume=1000,
            )
        )
    return tuple(out)


class IndexVendor:
    name = "indices"
    is_realtime = False
    capabilities = frozenset({DataDomain.INDICES})

    def quote(self, symbol: Symbol) -> Quote:
        raise NoMarketDataError(symbol.canonical)

    def bars(
        self,
        symbol: Symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> tuple[Bar, ...]:
        del interval, start, end
        if symbol.tradingsymbol == "INDIA VIX":
            return _bars(base=12.0, step=0.0)
        return _bars(base=20000.0, step=20.0)


def _index_router() -> VendorRouter:
    router = VendorRouter()
    router.register_vendor(DataDomain.INDICES, IndexVendor())
    return router


def test_market_regime_brief_is_deterministic() -> None:
    brief = MarketRegimeAgent(_index_router()).brief(as_of=NOW)
    assert brief.agent == "market_regime"
    assert brief.symbol == "INDEX:NIFTY 50"
    assert any("Regime: trend_up" in fact for fact in brief.facts)
    assert any("India VIX" in fact for fact in brief.facts)


class DailyVendor:
    name = "daily"
    is_realtime = False
    capabilities = frozenset({DataDomain.BARS_DAILY})

    def quote(self, symbol: Symbol) -> Quote:
        raise NoMarketDataError(symbol.canonical)

    def bars(
        self,
        symbol: Symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> tuple[Bar, ...]:
        del symbol, interval, start, end
        return _bars(base=100.0, step=0.6)


def _daily_router() -> VendorRouter:
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, DailyVendor())
    router.register_vendor(DataDomain.INDICES, IndexVendor())
    return router


class StubNews:
    def brief(self, symbol: Symbol, *, as_of: datetime) -> SpecialistBrief:
        return SpecialistBrief(
            agent="news",
            symbol=symbol.canonical,
            as_of=as_of,
            facts=("NEWS-SPECIAL-FACT",),
            source="stub",
        )


class StubRegime:
    def brief(self, *, as_of: datetime) -> SpecialistBrief:
        return SpecialistBrief(
            agent="market_regime",
            symbol="INDEX:NIFTY 50",
            as_of=as_of,
            facts=("REGIME-SPECIAL-FACT",),
            source="stub",
        )


class StubFundamental:
    def brief(self, symbol: Symbol, *, as_of: datetime) -> SpecialistBrief:
        return SpecialistBrief(
            agent="fundamental_events",
            symbol=symbol.canonical,
            as_of=as_of,
            facts=("EVENT-SPECIAL-FACT",),
            source="stub",
        )


def test_pipeline_collects_specialist_briefs_and_traces() -> None:
    pipeline = AnalysisPipeline(
        _daily_router(),
        clock=FrozenClock(),
        news=cast(NewsContextAgent, StubNews()),
        regime=cast(MarketRegimeAgent, StubRegime()),
        fundamental=cast(FundamentalEventsAgent, StubFundamental()),
    )
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000,
        available_cash_paise=1_000_000,
        risk_basis_points=100,
    )
    assert {brief.agent for brief in result.specialists} == {
        "news",
        "market_regime",
        "fundamental_events",
    }
    assert "specialists" in result.to_dict()
    nodes = {entry.node for entry in result.trace}
    assert {"market_regime", "fundamental_events"}.issubset(nodes)


class PromptCaller:
    def __init__(self) -> None:
        self.prompt = ""

    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        task_id: str = "",
        task_version: str = "",
    ) -> object:
        del system, task_id, task_version
        self.prompt = prompt
        from bq.llm_result import LLMResult

        return LLMResult(text='{"argument":"a","strongest_points":[]}')


def _report() -> TechnicalReport:
    indicator = IndicatorSnapshot(
        as_of=NOW,
        bar_count=120,
        close=100.0,
        sma_20=98.0,
        sma_50=95.0,
        ema_20=99.0,
        rsi_14=58.0,
        macd=None,
        bollinger=None,
        atr_14=2.0,
        vwap=97.0,
        adx_14=27.0,
        max_drawdown=0.1,
        volume=None,
        swings=None,
    )
    return TechnicalReport(
        symbol="NSE:RELIANCE",
        as_of=NOW,
        timeframe="swing",
        last_close=Decimal("100.00"),
        bar_count=120,
        trend=SubScore(0.5, ("trend",)),
        momentum=SubScore(0.3, ("momentum",)),
        volatility=SubScore(-0.2, ("volatility",)),
        structure=SubScore(0.4, ("structure",)),
        indicators=indicator,
    )


def test_debate_receives_specialist_facts() -> None:
    caller = PromptCaller()
    Debate(caller).bull(_report(), extra_facts=("REGIME-SPECIAL-FACT",))
    assert "REGIME-SPECIAL-FACT" in caller.prompt
