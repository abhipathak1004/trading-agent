from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from bq.agent_prompts import SUPER_AGENT_TEMPLATE, PromptStore
from bq.analysis.decision import (
    DataFreshness,
    OrderType,
    Product,
    Signal,
    TradePlan,
)
from bq.analysis.pipeline import AnalysisResult
from bq.analysis.technical import SubScore, TechnicalReport
from bq.compute.indicators import IndicatorSnapshot
from bq.infra.database import migrate
from bq.llm_client import LLMNotConfigured
from bq.llm_result import LLMResult
from bq.llm_service import CompletionOutcome
from bq.memory import PlaybookStore
from bq.settings import Provider
from bq.super_agent import RouterLike, StrategyError, SuperAgent

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _report(symbol: str, composite: float) -> TechnicalReport:
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
        symbol=symbol,
        as_of=NOW,
        timeframe="swing",
        last_close=Decimal("100.00"),
        bar_count=120,
        trend=SubScore(composite, ("trend",)),
        momentum=SubScore(composite, ("momentum",)),
        volatility=SubScore(composite, ("volatility",)),
        structure=SubScore(composite, ("structure",)),
        indicators=indicator,
    )


def _plan(symbol: str, *, quantity: int = 10) -> TradePlan:
    return TradePlan(
        signal=Signal.BUY,
        symbol=symbol,
        exchange="NSE",
        product=Product.CNC,
        order_type=OrderType.LIMIT,
        entry_price=Decimal("100.00"),
        stop_loss=Decimal("95.00"),
        target_price=Decimal("110.00"),
        suggested_quantity=quantity,
        confidence=0.7,
        timeframe="swing",
        invalidation_reason="close below support",
        data_freshness=DataFreshness.DELAYED_YAHOO,
        as_of=NOW,
    )


def _result(symbol: str, *, composite: float, plan: TradePlan | None) -> AnalysisResult:
    return AnalysisResult(
        symbol=symbol,
        timeframe="swing",
        as_of=NOW,
        report=_report(symbol, composite),
        plan=plan,
        mode="RULES_ONLY",
        debate_rounds=0,
        vendor_daily="yahoo",
        vendor_quotes="yahoo",
        is_realtime=False,
        bull=None,
        bear=None,
        verdict=None,
        risk=None,
        vetoes=() if plan is not None else ("no_deterministic_long_case",),
        notes=(),
        input_tokens=None,
        output_tokens=None,
        skipped_nodes=(),
    )


class FakePipeline:
    def __init__(self, results: dict[str, AnalysisResult]) -> None:
        self.results = results

    def run(self, *, symbol: object, **_kwargs: object) -> AnalysisResult:
        return self.results[symbol.canonical]  # type: ignore[attr-defined]


class FakeRouter:
    def __init__(self, *, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.system = ""
        self.prompt = ""

    def complete(
        self,
        agent_id: str,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str = "v1",
        timeout: float | None = None,
    ) -> CompletionOutcome:
        del agent_id, task_id, task_version, timeout
        self.system = system
        self.prompt = prompt
        if self.error is not None:
            raise self.error
        return CompletionOutcome(
            provider=Provider.GEMINI,
            model="gemini-2.5-pro",
            result=LLMResult(text=self.text),
        )


def _no_router() -> RouterLike:
    raise LLMNotConfigured("No reasoning model is configured.")


def _prompts(tmp_path: Path) -> PromptStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return PromptStore(path, FrozenClock())


def _agent(
    tmp_path: Path,
    results: dict[str, AnalysisResult],
    *,
    router: FakeRouter | None = None,
) -> SuperAgent:
    return SuperAgent(
        pipeline_factory=lambda: FakePipeline(results),
        router_factory=(
            (lambda: cast(RouterLike, router)) if router is not None else _no_router
        ),
        prompts=_prompts(tmp_path),
        clock=FrozenClock(),
    )


def test_rules_synthesis_accepts_only_deterministic_buys(tmp_path: Path) -> None:
    agent = _agent(
        tmp_path,
        {
            "NSE:AAA": _result("NSE:AAA", composite=0.6, plan=_plan("NSE:AAA")),
            "NSE:BBB": _result("NSE:BBB", composite=0.2, plan=None),
        },
    )
    run = agent.run(
        strategy="Buy the strongest momentum name.",
        symbols="NSE:AAA, NSE:BBB",
        timeframe="swing",
        equity_paise=1_000_000,
        available_cash_paise=1_000_000,
        risk_basis_points=100,
        max_positions=2,
    )
    assert run.decision.source == "rules"
    assert [item.symbol for item in run.accepted] == ["NSE:AAA"]
    assert run.accepted[0].plan.suggested_quantity == 10
    assert any(
        item.symbol == "NSE:BBB" and item.action == "HOLD" for item in run.rejected
    )
    assert [action.symbol for action in run.decision.ranked_actions] == [
        "NSE:AAA",
        "NSE:BBB",
    ]


def test_cash_gate_rejects_a_plan_that_does_not_fit(tmp_path: Path) -> None:
    agent = _agent(
        tmp_path,
        {
            "NSE:AAA": _result("NSE:AAA", composite=0.6, plan=_plan("NSE:AAA")),
            "NSE:BBB": _result("NSE:BBB", composite=0.5, plan=_plan("NSE:BBB")),
        },
    )
    run = agent.run(
        strategy="Own the two best setups.",
        symbols="NSE:AAA, NSE:BBB",
        timeframe="swing",
        equity_paise=1_000_000,
        available_cash_paise=150_000,
        risk_basis_points=100,
        max_positions=2,
    )
    assert [item.symbol for item in run.accepted] == ["NSE:AAA"]
    assert any("Insufficient cash" in item.reason for item in run.rejected)


def test_super_agent_can_downgrade_but_not_create_a_buy(tmp_path: Path) -> None:
    router = FakeRouter(
        text=(
            '{"strategy_summary":"Momentum book.",'
            '"ranked_actions":['
            '{"symbol":"NSE:AAA","action":"BUY","rank":1,"size_scale":0.5,"rationale":"best"},'
            '{"symbol":"NSE:BBB","action":"HOLD","rank":2,"size_scale":1.0,"rationale":"weaker"},'
            '{"symbol":"NSE:ZZZ","action":"BUY","rank":3,"size_scale":1.0,"rationale":"ghost"}'
            '],"portfolio_notes":"One position.","risk_notes":"Long-only."}'
        )
    )
    agent = _agent(
        tmp_path,
        {
            "NSE:AAA": _result("NSE:AAA", composite=0.6, plan=_plan("NSE:AAA")),
            "NSE:BBB": _result("NSE:BBB", composite=0.5, plan=_plan("NSE:BBB")),
        },
        router=router,
    )
    run = agent.run(
        strategy="Own the two best setups.",
        symbols="NSE:AAA, NSE:BBB",
        timeframe="swing",
        equity_paise=1_000_000,
        available_cash_paise=1_000_000,
        risk_basis_points=100,
        max_positions=2,
    )
    assert run.decision.source == "reasoning"
    assert run.decision.model == "gemini-2.5-pro"
    assert [item.symbol for item in run.accepted] == ["NSE:AAA"]
    assert run.accepted[0].size_scale == 0.5
    assert any(
        item.symbol == "NSE:BBB" and "Super Agent" in item.reason
        for item in run.rejected
    )
    assert all(action.symbol != "NSE:ZZZ" for action in run.decision.ranked_actions)


def test_reasoning_failure_degrades_to_rules(tmp_path: Path) -> None:
    router = FakeRouter(error=RuntimeError("provider down"))
    agent = _agent(
        tmp_path,
        {"NSE:AAA": _result("NSE:AAA", composite=0.6, plan=_plan("NSE:AAA"))},
        router=router,
    )
    run = agent.run(
        strategy="Buy the strongest name.",
        symbols="NSE:AAA",
        timeframe="swing",
        equity_paise=1_000_000,
        available_cash_paise=1_000_000,
        risk_basis_points=100,
        max_positions=1,
    )
    assert run.decision.source == "rules"
    assert run.decision.fallback_used is True
    assert run.decision.fallback_from == "reasoning"
    assert [item.symbol for item in run.accepted] == ["NSE:AAA"]


def test_super_agent_prompt_is_overridable(tmp_path: Path) -> None:
    store = _prompts(tmp_path)
    store.save(
        "super_agent_cio",
        system="CUSTOM CIO SYSTEM",
        template=SUPER_AGENT_TEMPLATE,
    )
    router = FakeRouter(
        text=(
            '{"strategy_summary":"s","ranked_actions":['
            '{"symbol":"NSE:AAA","action":"BUY","rank":1,"size_scale":1.0,"rationale":"r"}'
            '],"portfolio_notes":"p","risk_notes":"k"}'
        )
    )
    agent = SuperAgent(
        pipeline_factory=lambda: FakePipeline(
            {"NSE:AAA": _result("NSE:AAA", composite=0.6, plan=_plan("NSE:AAA"))}
        ),
        router_factory=lambda: cast(RouterLike, router),
        prompts=store,
        clock=FrozenClock(),
    )
    agent.run(
        strategy="Buy it.",
        symbols="NSE:AAA",
        timeframe="swing",
        equity_paise=1_000_000,
        available_cash_paise=1_000_000,
        risk_basis_points=100,
        max_positions=1,
    )
    assert router.system.startswith("CUSTOM CIO SYSTEM")


def test_invalid_strategy_requests_are_rejected(tmp_path: Path) -> None:
    agent = _agent(
        tmp_path,
        {"NSE:AAA": _result("NSE:AAA", composite=0.6, plan=_plan("NSE:AAA"))},
    )
    with pytest.raises(StrategyError, match="required"):
        agent.run(
            strategy="",
            symbols="NSE:AAA",
            timeframe="swing",
            equity_paise=1,
            available_cash_paise=0,
            risk_basis_points=100,
            max_positions=1,
        )
    with pytest.raises(StrategyError, match="Indices"):
        agent.run(
            strategy="Long the index.",
            symbols="NIFTY",
            timeframe="swing",
            equity_paise=1,
            available_cash_paise=0,
            risk_basis_points=100,
            max_positions=1,
        )


def test_super_agent_uses_a_selected_playbook(tmp_path: Path) -> None:
    store = _prompts(tmp_path)
    playbooks = PlaybookStore(store.path, FrozenClock())
    router = FakeRouter(
        text=(
            '{"strategy_summary":"s","ranked_actions":['
            '{"symbol":"NSE:AAA","action":"BUY","rank":1,"size_scale":1.0,"rationale":"r"}'
            '],"portfolio_notes":"p","risk_notes":"k"}'
        )
    )
    agent = SuperAgent(
        pipeline_factory=lambda: FakePipeline(
            {"NSE:AAA": _result("NSE:AAA", composite=0.6, plan=_plan("NSE:AAA"))}
        ),
        router_factory=lambda: cast(RouterLike, router),
        prompts=store,
        playbooks=playbooks,
        clock=FrozenClock(),
    )
    run = agent.run(
        strategy="Protect capital.",
        symbols="NSE:AAA",
        timeframe="swing",
        equity_paise=1_000_000,
        available_cash_paise=1_000_000,
        risk_basis_points=100,
        max_positions=1,
        playbook_id="defensive",
    )
    assert run.spec.playbook == "defensive"
    assert "Capital preservation" in router.prompt
