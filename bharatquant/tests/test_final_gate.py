from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from bq.analysis.decision import (
    DataFreshness,
    OrderType,
    Product,
    Signal,
    TradePlan,
)
from bq.analysis.final_gate import DecisionRouterLike, FinalGate
from bq.analysis.pipeline import AnalysisPipeline
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
from bq.decision.policy import DecisionPolicy
from bq.decision.state import DecisionState
from bq.decision.types import Answer, DecisionOutcome, JudgmentSet, QuestionType

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


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


def _plan() -> TradePlan:
    return TradePlan(
        signal=Signal.BUY,
        symbol="NSE:RELIANCE",
        exchange="NSE",
        product=Product.CNC,
        order_type=OrderType.LIMIT,
        entry_price=Decimal("100.00"),
        stop_loss=Decimal("95.00"),
        target_price=Decimal("110.00"),
        suggested_quantity=10,
        confidence=0.7,
        timeframe="swing",
        invalidation_reason="close below support",
        data_freshness=DataFreshness.DELAYED_YAHOO,
        as_of=NOW,
    )


def _judgments(
    *, action: str = "BUY", probability: float = 0.8, conviction: int = 4
) -> JudgmentSet:
    return JudgmentSet(
        battery="final_gate",
        answers=(
            (
                "final_action",
                Answer(kind=QuestionType.CHOICE, choice=action, confidence=0.9),
            ),
            ("execute_trade", Answer(kind=QuestionType.NOUL, noul=probability)),
            (
                "conviction_tier",
                Answer(kind=QuestionType.SCORE, score=conviction, confidence=0.8),
            ),
        ),
        model="jev-1.14.0",
        source="jev_native",
    )


class FakeRouter:
    def __init__(self, judgments: JudgmentSet) -> None:
        self.judgments = judgments
        self.state: DecisionState | None = None

    def decide(
        self,
        agent_id: str,
        *,
        state: DecisionState,
        battery: str,
        task_id: str = "",
        claims: tuple[str, ...] = (),
    ) -> DecisionOutcome:
        del agent_id, battery, task_id, claims
        self.state = state
        return DecisionOutcome(
            judgments=self.judgments,
            provider="TYPESAFE_JEV",
            model="jev-1.14.0",
            tier="decision",
            state_hash=state.digest(),
        )


def test_final_gate_executes_and_embeds_the_plan_in_state() -> None:
    router = FakeRouter(_judgments())
    gate = FinalGate(cast(DecisionRouterLike, router), DecisionPolicy())
    result = gate.evaluate(_report(), _plan(), data_freshness="delayed_yfinance")
    assert result.decision.execute is True
    assert result.decision.verdict.value == "BUY"
    assert result.decision.size_scale == 1.0
    payload = result.to_dict()
    assert payload["source"] == "jev_native"
    assert payload["action"] == "BUY"
    assert result.state.to_state_dict()["has_plan"] is True
    assert result.state.to_state_dict()["proposed_action"] == "BUY"
    assert result.state.to_state_dict()["entry_price"] == 100.0


def test_final_gate_blocks_a_low_probability_or_hold() -> None:
    policy = DecisionPolicy()
    low = FinalGate(
        cast(DecisionRouterLike, FakeRouter(_judgments(probability=0.4))), policy
    ).evaluate(_report(), _plan(), data_freshness="delayed_yfinance")
    assert low.decision.execute is False
    hold = FinalGate(
        cast(DecisionRouterLike, FakeRouter(_judgments(action="HOLD"))), policy
    ).evaluate(_report(), _plan(), data_freshness="delayed_yfinance")
    assert hold.decision.execute is False


def _bars() -> tuple[Bar, ...]:
    start = NOW - timedelta(days=120)
    out = []
    for index in range(120):
        price = Decimal(str(round(100.0 + index * 0.6, 2)))
        out.append(
            Bar(
                event_at=start + timedelta(days=index),
                open=price,
                high=price + Decimal("0.5"),
                low=price - Decimal("0.5"),
                close=price,
                volume=1000,
            )
        )
    return tuple(out)


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
        return _bars()


def _router() -> VendorRouter:
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, DailyVendor())
    return router


def test_pipeline_records_the_final_gate() -> None:
    gate = FinalGate(
        cast(DecisionRouterLike, FakeRouter(_judgments())), DecisionPolicy()
    )
    pipeline = AnalysisPipeline(_router(), final_gate=gate)
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000,
        available_cash_paise=1_000_000,
        risk_basis_points=100,
    )
    assert result.plan is not None
    assert result.final_gate is not None
    assert result.final_gate.decision.execute is True
    assert "final_gate" in {entry.node for entry in result.trace}
    assert result.to_dict()["final_gate"] is not None
