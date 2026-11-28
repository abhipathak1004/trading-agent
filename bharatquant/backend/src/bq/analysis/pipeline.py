"""The analysis pipeline: data -> technical -> debate -> plan.

Node graph, run by the existing :class:`bq.agents.AgentDAG`:

    gather_data  ─┐
                  ├→ technical → bull ─┐
    news_context ┘               bear ─┴→ judge → risk_team → size → decision

``gather_data`` and ``news_context`` are independent roots, so they run first and
in the same layer. ``bull`` and ``bear`` are siblings and run in the next layer
after ``technical``. The LLM nodes (``judge``, ``risk_team``) are *optional*: when
the provider is down they are skipped and the run continues with a code-derived
invalidation reason, recorded as ``RULES_ONLY``. That is plan §3's graceful
degradation applied concretely.

``size`` and ``decision`` are pure functions of the deterministic report, so a
model outage cannot change position size — it can only remove commentary.

**Look-ahead guard.** ``as_of`` is threaded through every node. Bars later than
``as_of`` are dropped in ``gather_data`` *and* again in the analyst, so a caller
that forgets cannot leak the future.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from bq.agents import AgentCycleResult, AgentDAG, AgentDAGError, AgentNode
from bq.analysis.briefs import SPECIALIST_ORDER, SpecialistBrief
from bq.analysis.confidence_engine import ConfidenceEngine, ConfidenceResult
from bq.analysis.debate import (
    Argument,
    Debate,
    DebateUnavailable,
    Verdict,
    confidence_ceiling,
)
from bq.analysis.decision import (
    DataFreshness,
    Product,
    Signal,
    TradePlan,
    TradePlanError,
    build_plan,
)
from bq.analysis.evidence import EvidenceResult, EvidenceVerifier
from bq.analysis.final_gate import FinalGate, FinalGateResult
from bq.analysis.fundamental import FundamentalEventsAgent
from bq.analysis.news_context import NewsContextAgent
from bq.analysis.regime import MarketRegimeAgent
from bq.analysis.risk_team import (
    RiskAnalyst,
    RiskAssessment,
    deterministic_invalidation,
)
from bq.analysis.technical import TechnicalAnalyst, TechnicalReport
from bq.core import Clock, SystemClock
from bq.data.symbols import Symbol
from bq.data.vendors.base import Bar, BarInterval
from bq.data.vendors.router import VendorResult, VendorRouter
from bq.model_router import RoutedModelCaller, RouteUse

DEBATE_ROUNDS = {"intraday": 1, "swing": 2}
MIN_BARS_SWING = 60
MIN_BARS_INTRADAY = 30


class PipelineError(RuntimeError):
    """A required stage failed. Optional stages never raise this."""


@dataclass(slots=True)
class PipelineContext:
    """Mutable state carried between nodes. One instance per run."""

    symbol: Symbol
    timeframe: str
    as_of: datetime
    interval: BarInterval
    lookback_days: int
    started_at: datetime
    bars: tuple[Bar, ...] = ()
    vendor_daily: str = ""
    vendor_quotes: str = ""
    is_realtime: bool = False
    news_facts: tuple[str, ...] = ()
    specialists: dict[str, SpecialistBrief] = field(default_factory=dict)
    evidence: EvidenceResult | None = None
    report: TechnicalReport | None = None
    bull: Argument | None = None
    bear: Argument | None = None
    verdict: Verdict | None = None
    risk: RiskAssessment | None = None
    ceiling: float = 0.0
    debate_rounds_run: int = 0
    debate_attempted: bool = False
    mode: str = "FULL"
    plan: TradePlan | None = None
    final_gate: FinalGateResult | None = None
    confidence: ConfidenceResult | None = None
    vetoes: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    trace: list[AgentTrace] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})

    @property
    def data_freshness(self) -> DataFreshness:
        return (
            DataFreshness.REALTIME_KITE
            if self.is_realtime
            else DataFreshness.DELAYED_YAHOO
        )

    @property
    def specialist_facts(self) -> tuple[str, ...]:
        facts: list[str] = []
        for name in SPECIALIST_ORDER:
            brief = self.specialists.get(name)
            if brief is not None:
                facts.extend(brief.facts)
        return tuple(facts[:18])


@dataclass(frozen=True, slots=True)
class AgentTrace:
    """One agent node's contribution, for the UI's orchestration view.

    ``status`` is one of ``ok``, ``degraded`` (ran but the model was unavailable)
    or ``failed``. ``summary`` is a short, human-readable line; ``detail`` carries
    structured output for the panel. Nothing here is model-written prose unless
    the node itself is an LLM node, and then it is bounded.
    """

    node: str
    role: str
    status: str
    summary: str

    def __post_init__(self) -> None:
        if self.status not in {"ok", "degraded", "failed"}:
            raise ValueError("Unknown agent trace status.")
        if not self.node or not self.role:
            raise ValueError("An agent trace needs a node and a role.")

    def to_dict(self) -> dict[str, object]:
        return {
            "node": self.node,
            "role": self.role,
            "status": self.status,
            "summary": self.summary,
        }


AGENT_ROLES: dict[str, str] = {
    "gather_data": "Market data",
    "news_context": "News context",
    "market_regime": "Market regime",
    "fundamental_events": "Fundamental & events",
    "evidence_verifier": "Evidence verifier",
    "technical": "Technical analyst",
    "bull": "Bull advocate",
    "bear": "Bear advocate",
    "judge": "Research judge",
    "risk_team": "Risk analyst",
    "size": "Position sizer",
    "decision": "Decision",
    "final_gate": "Final gate",
    "confidence": "Confidence engine",
}

# The order agents appear in the UI. This is the graph order, not DAG sort order,
# so the panel reads as a pipeline rather than an alphabetised list.
AGENT_ORDER: tuple[str, ...] = (
    "gather_data",
    "news_context",
    "market_regime",
    "fundamental_events",
    "evidence_verifier",
    "technical",
    "bull",
    "bear",
    "judge",
    "risk_team",
    "size",
    "decision",
    "final_gate",
    "confidence",
)


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """What one pipeline run produces."""

    symbol: str
    timeframe: str
    as_of: datetime
    report: TechnicalReport
    plan: TradePlan | None
    mode: str
    debate_rounds: int
    vendor_daily: str
    vendor_quotes: str
    is_realtime: bool
    bull: Argument | None
    bear: Argument | None
    verdict: Verdict | None
    risk: RiskAssessment | None
    vetoes: tuple[str, ...]
    notes: tuple[str, ...]
    input_tokens: int | None
    output_tokens: int | None
    skipped_nodes: tuple[str, ...]
    trace: tuple[AgentTrace, ...] = ()
    model_routing: tuple[RouteUse, ...] = ()
    specialists: tuple[SpecialistBrief, ...] = ()
    evidence: EvidenceResult | None = None
    final_gate: FinalGateResult | None = None
    confidence: ConfidenceResult | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "as_of": self.as_of.isoformat(),
            "mode": self.mode,
            "debate_rounds": self.debate_rounds,
            "data_provenance": {
                "vendor_daily": self.vendor_daily,
                "vendor_quotes": self.vendor_quotes,
                "is_realtime": self.is_realtime,
                "data_freshness": (
                    "realtime_kite" if self.is_realtime else "delayed_yfinance"
                ),
            },
            "report": self.report.to_dict(),
            "plan": None if self.plan is None else self.plan.to_dict(),
            "bull": None if self.bull is None else self.bull.to_dict(),
            "bear": None if self.bear is None else self.bear.to_dict(),
            "verdict": None if self.verdict is None else self.verdict.to_dict(),
            "risk": None if self.risk is None else self.risk.to_dict(),
            "vetoes": list(self.vetoes),
            "notes": list(self.notes),
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            },
            "skipped_nodes": list(self.skipped_nodes),
            "agents": [entry.to_dict() for entry in self.trace],
            "model_routing": [use.to_dict() for use in self.model_routing],
            "specialists": [brief.to_dict() for brief in self.specialists],
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "final_gate": (
                None if self.final_gate is None else self.final_gate.to_dict()
            ),
            "confidence": (
                None if self.confidence is None else self.confidence.to_dict()
            ),
        }


def _signal_for(report: TechnicalReport, verdict: Verdict | None) -> Signal:
    """Decide the signal from deterministic evidence, with the judge as a veto.

    A model can only ever *lower* a signal: the deterministic composite must be
    positive before BUY is considered, and the judge may downgrade it to HOLD.
    It can never create a BUY on its own.
    """
    composite = report.composite
    if composite is None or composite <= 0.0:
        return Signal.HOLD
    if report.trend is None or report.trend.value <= 0.0:
        return Signal.HOLD
    if verdict is not None and verdict.verdict != "BUY":
        return Signal.HOLD
    return Signal.BUY


def _stops_from_report(
    report: TechnicalReport, *, risk_basis_points: int
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Derive entry/stop/target from ATR and structure. Deterministic only.

    All three prices are rounded to two decimal places — the tick size of an
    Indian equity is a paisa, and a 16-decimal stop is not a price anyone can
    place. Rounding the *stop down* and the *target down* keeps the plan
    conservative: rounding either away from the entry would widen risk.
    """
    entry = report.last_close.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    atr = report.indicators.atr_14
    swings = report.indicators.swings
    if atr is None or atr <= 0:
        return None
    atr_decimal = Decimal(str(atr))

    # Stop: the swing support when it sits inside a sensible ATR band, otherwise
    # 1.5 ATR. A support far below price is not a stop — it is a different trade
    # with much larger risk — so it is bounded at 1.5 ATR.
    atr_stop = entry - atr_decimal * Decimal("1.5")
    if swings is not None and swings.support is not None:
        support = Decimal(str(swings.support))
        stop = support if atr_stop <= support < entry else atr_stop
    else:
        stop = atr_stop
    stop = stop.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    if stop >= entry:
        # A "stop" at or above entry is not a long stop.
        return None

    risk = entry - stop
    if risk <= 0:
        return None
    # Target: the configured reward/risk floor applied to the rounded risk, so
    # the reported ratio is exactly what the prices imply.
    ratio = Decimal(risk_basis_points) / Decimal("10000")
    target = (entry + risk * max(ratio, Decimal("1.5"))).quantize(
        Decimal("0.01"), rounding="ROUND_HALF_UP"
    )
    return entry, stop, target


class AnalysisPipeline:
    """Runs the DAG for one symbol. Deterministic where it matters."""

    def __init__(
        self,
        router: VendorRouter,
        *,
        debate: Debate | None = None,
        risk_analyst: RiskAnalyst | None = None,
        analyst: TechnicalAnalyst | None = None,
        clock: Clock | None = None,
        routing: RoutedModelCaller | None = None,
        news: NewsContextAgent | None = None,
        regime: MarketRegimeAgent | None = None,
        fundamental: FundamentalEventsAgent | None = None,
        final_gate: FinalGate | None = None,
        evidence: EvidenceVerifier | None = None,
        confidence: ConfidenceEngine | None = None,
    ) -> None:
        self.router = router
        self.debate = debate
        self.risk_analyst = risk_analyst
        self.analyst = analyst or TechnicalAnalyst()
        self.clock = clock or SystemClock()
        self.routing = routing
        self.news = news
        self.regime = regime
        self.fundamental = fundamental
        self.final_gate = final_gate
        self.evidence = evidence
        self.confidence = confidence

    # -- nodes ------------------------------------------------------------

    def _gather_data(self, ctx: PipelineContext) -> None:
        end = ctx.as_of
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        start = start.fromtimestamp(
            start.timestamp() - ctx.lookback_days * 86400, tz=UTC
        )
        try:
            result: VendorResult[tuple[Bar, ...]] = self.router.bars(
                ctx.symbol, interval=ctx.interval, start=start, end=end
            )
        except Exception as exc:
            raise PipelineError(
                f"No market data for {ctx.symbol.canonical}: {type(exc).__name__}."
            ) from exc
        bars = tuple(bar for bar in result.data if bar.event_at <= end)
        if not bars:
            raise PipelineError(
                f"No bars at or before as_of for {ctx.symbol.canonical}."
            )
        ctx.bars = bars
        ctx.vendor_daily = result.source
        ctx.is_realtime = result.is_realtime
        ctx.vendor_quotes = result.source
        freshness = "real-time" if result.is_realtime else "delayed"
        self._record(
            ctx,
            "gather_data",
            "ok",
            f"{len(bars)} bars from {result.source} ({freshness})",
        )

    def _news_context(self, ctx: PipelineContext) -> None:
        if self.news is None:
            ctx.news_facts = ()
            ctx.notes.append("news context: not configured")
            self._record(ctx, "news_context", "degraded", "not configured")
            return
        try:
            brief = self.news.brief(ctx.symbol, as_of=ctx.as_of)
        except Exception as exc:  # an optional specialist must not kill a run
            ctx.notes.append(f"news context: unavailable ({type(exc).__name__})")
            self._record(
                ctx,
                "news_context",
                "degraded",
                f"news unavailable ({type(exc).__name__})",
            )
            return
        ctx.specialists["news"] = brief
        ctx.news_facts = brief.facts
        self._record(
            ctx,
            "news_context",
            "degraded" if brief.degraded else "ok",
            f"{len(brief.facts)} fact(s) from {brief.source or 'news'}",
        )

    def _market_regime(self, ctx: PipelineContext) -> None:
        if self.regime is None:
            self._record(ctx, "market_regime", "degraded", "not configured")
            return
        try:
            brief = self.regime.brief(as_of=ctx.as_of)
        except Exception as exc:
            ctx.notes.append(f"market regime: unavailable ({type(exc).__name__})")
            self._record(
                ctx,
                "market_regime",
                "degraded",
                f"regime unavailable ({type(exc).__name__})",
            )
            return
        ctx.specialists["market_regime"] = brief
        self._record(
            ctx,
            "market_regime",
            "degraded" if brief.degraded else "ok",
            f"{len(brief.facts)} fact(s) from {brief.source or 'indices'}",
        )

    def _fundamental_events(self, ctx: PipelineContext) -> None:
        if self.fundamental is None:
            self._record(ctx, "fundamental_events", "degraded", "not configured")
            return
        try:
            brief = self.fundamental.brief(ctx.symbol, as_of=ctx.as_of)
        except Exception as exc:
            ctx.notes.append(f"fundamental events: unavailable ({type(exc).__name__})")
            self._record(
                ctx,
                "fundamental_events",
                "degraded",
                f"events unavailable ({type(exc).__name__})",
            )
            return
        ctx.specialists["fundamental_events"] = brief
        self._record(
            ctx,
            "fundamental_events",
            "degraded" if brief.degraded else "ok",
            f"{len(brief.facts)} fact(s) from {brief.source or 'news'}",
        )

    def _final_gate(self, ctx: PipelineContext) -> None:
        if self.final_gate is None:
            self._record(ctx, "final_gate", "degraded", "not configured")
            return
        if ctx.report is None or ctx.plan is None:
            self._record(ctx, "final_gate", "degraded", "skipped: no plan")
            return
        try:
            gate = self.final_gate.evaluate(
                ctx.report,
                ctx.plan,
                data_freshness=(
                    "realtime_kite" if ctx.is_realtime else "delayed_yfinance"
                ),
            )
        except Exception as exc:
            ctx.notes.append(f"final gate: unavailable ({type(exc).__name__})")
            self._record(
                ctx,
                "final_gate",
                "degraded",
                f"gate unavailable ({type(exc).__name__})",
            )
            return
        ctx.final_gate = gate
        execute = gate.outcome.judgments.answer("execute_trade")
        probability = execute.noul if execute.noul is not None else 0.0
        self._record(
            ctx,
            "final_gate",
            "ok",
            f"{gate.decision.verdict.value} p_exec {probability:.2f} "
            f"({gate.outcome.source})",
        )
        if not gate.decision.execute:
            ctx.vetoes = (
                *ctx.vetoes,
                f"final_gate:{gate.decision.reason}",
            )
            ctx.notes.append(f"final gate: {gate.decision.reason}")

    def _evidence_verifier(self, ctx: PipelineContext) -> None:
        if self.evidence is None:
            self._record(ctx, "evidence_verifier", "degraded", "not configured")
            return
        try:
            result = self.evidence.verify(ctx.specialists, as_of=ctx.as_of)
        except Exception as exc:
            ctx.notes.append(f"evidence verifier: unavailable ({type(exc).__name__})")
            self._record(
                ctx,
                "evidence_verifier",
                "degraded",
                f"verification unavailable ({type(exc).__name__})",
            )
            return
        ctx.specialists = dict(result.briefs)
        ctx.evidence = result
        status = "ok" if result.unverified_claims == 0 else "degraded"
        self._record(
            ctx,
            "evidence_verifier",
            status,
            f"{result.verified_claims} verified, "
            f"{result.unverified_claims} unverified claim(s)",
        )

    def _confidence(self, ctx: PipelineContext) -> None:
        if self.confidence is None:
            self._record(ctx, "confidence", "degraded", "not configured")
            return
        if ctx.report is None or ctx.plan is None:
            self._record(ctx, "confidence", "degraded", "skipped: no plan")
            return
        try:
            result = self.confidence.assess(
                bars=ctx.bars,
                report=ctx.report,
                plan=ctx.plan,
                specialists=tuple(ctx.specialists.values()),
                as_of=ctx.as_of,
            )
        except Exception as exc:  # noqa: BLE001 - optional confidence must not kill a run
            ctx.notes.append(f"confidence: unavailable ({type(exc).__name__})")
            self._record(
                ctx,
                "confidence",
                "degraded",
                f"confidence unavailable ({type(exc).__name__})",
            )
            return
        if result is None:
            self._record(ctx, "confidence", "degraded", "no long candidate")
            return
        ctx.confidence = result
        self._record(
            ctx,
            "confidence",
            "ok",
            f"{result.assessment.level.value} p={result.assessment.probability:.2f} "
            f"base n={result.assessment.base_rate_samples}",
        )

    def _technical(self, ctx: PipelineContext) -> None:
        if not ctx.bars:
            raise PipelineError("Technical stage ran without bars.")
        ctx.report = self.analyst.analyse(
            symbol=ctx.symbol.canonical,
            bars=ctx.bars,
            timeframe=ctx.timeframe,
            as_of=ctx.as_of,
        )
        composite = ctx.report.composite
        scored = ctx.report.available_dimensions
        self._record(
            ctx,
            "technical",
            "ok" if composite is not None else "degraded",
            (
                f"{scored}/4 dimensions scored, composite {composite:+.2f}"
                if composite is not None
                else "insufficient history for any scored dimension"
            ),
        )

    # The debate nodes *record* degradation rather than raising. Sizing must not
    # depend on the model being reachable: if these raised, the DAG would skip
    # `size` and `decision` too, and a provider outage would silently stop the
    # only stage that matters. So an unavailable debate sets a flag and returns.

    def _record(
        self, ctx: PipelineContext, node: str, status: str, summary: str
    ) -> None:
        ctx.trace.append(
            AgentTrace(
                node=node,
                role=AGENT_ROLES.get(node, node),
                status=status,
                summary=summary,
            )
        )

    def _degrade(self, ctx: PipelineContext, node: str, exc: Exception) -> None:
        ctx.degraded.append(node)
        ctx.mode = "RULES_ONLY"
        ctx.notes.append(f"{node}: unavailable ({type(exc).__name__})")
        self._record(ctx, node, "degraded", f"model unavailable ({type(exc).__name__})")

    def _bull(self, ctx: PipelineContext) -> None:
        if ctx.report is None or self.debate is None:
            self._degrade(ctx, "bull", DebateUnavailable("no debate configured"))
            return
        try:
            ctx.bull = self.debate.bull(ctx.report, extra_facts=ctx.specialist_facts)
            ctx.debate_attempted = True
            points = len(ctx.bull.points)
            self._record(ctx, "bull", "ok", f"bull case argued ({points} key point(s))")
        except DebateUnavailable as exc:
            self._degrade(ctx, "bull", exc)

    def _bear(self, ctx: PipelineContext) -> None:
        if ctx.report is None or self.debate is None:
            self._degrade(ctx, "bear", DebateUnavailable("no debate configured"))
            return
        try:
            ctx.bear = self.debate.bear(ctx.report, extra_facts=ctx.specialist_facts)
            ctx.debate_attempted = True
            points = len(ctx.bear.points)
            self._record(ctx, "bear", "ok", f"bear case argued ({points} key point(s))")
        except DebateUnavailable as exc:
            self._degrade(ctx, "bear", exc)

    def _judge(self, ctx: PipelineContext) -> None:
        if (
            ctx.report is None
            or self.debate is None
            or ctx.bull is None
            or ctx.bear is None
        ):
            self._degrade(
                ctx, "judge", DebateUnavailable("the debate did not complete")
            )
            return
        ctx.ceiling = confidence_ceiling(ctx.report)
        if ctx.ceiling <= 0.0:
            self._degrade(ctx, "judge", DebateUnavailable("ceiling is zero"))
            return
        try:
            ctx.verdict = self.debate.judge(
                ctx.report,
                bull=ctx.bull,
                bear=ctx.bear,
                ceiling=ctx.ceiling,
                extra_facts=ctx.specialist_facts,
            )
            ctx.debate_rounds_run += 1
            clamped = (
                " (clamped to the deterministic ceiling)" if ctx.verdict.clamped else ""
            )
            self._record(
                ctx,
                "judge",
                "ok",
                f"verdict {ctx.verdict.verdict} at confidence "
                f"{ctx.verdict.confidence:.2f}{clamped}",
            )
        except DebateUnavailable as exc:
            self._degrade(ctx, "judge", exc)

    def _risk_team(self, ctx: PipelineContext) -> None:
        if ctx.report is None or self.risk_analyst is None:
            self._degrade(ctx, "risk_team", DebateUnavailable("no risk analyst"))
            return
        try:
            ctx.risk = self.risk_analyst.assess(
                ctx.report, extra_facts=ctx.specialist_facts
            )
            ctx.debate_rounds_run += 1
            warnings = len(ctx.risk.early_warnings)
            self._record(
                ctx,
                "risk_team",
                "ok",
                f"advisory assessment ({warnings} early warning(s))",
            )
        except DebateUnavailable as exc:
            self._degrade(ctx, "risk_team", exc)

    def _size(
        self, ctx: PipelineContext, *, equity: int, cash: int, risk_bps: int
    ) -> None:
        """Deterministic sizing and plan assembly. Never optional."""
        assert ctx.report is not None
        signal = _signal_for(ctx.report, ctx.verdict)
        if signal is not Signal.BUY:
            ctx.vetoes = ctx.vetoes + ("no_deterministic_long_case",)
            composite = ctx.report.composite
            self._record(
                ctx,
                "size",
                "ok",
                (
                    f"no long case: composite {composite:+.2f} does not support a BUY"
                    if composite is not None
                    else "no long case: no scored dimensions"
                ),
            )
            return
        levels = _stops_from_report(ctx.report, risk_basis_points=15_000)
        if levels is None:
            ctx.vetoes = ctx.vetoes + ("no_valid_stop_distance",)
            self._record(ctx, "size", "degraded", "no valid stop distance")
            return
        entry, stop, target = levels
        invalidation = (
            ctx.risk.invalidation_reason
            if ctx.risk is not None
            else deterministic_invalidation(ctx.report)
        )
        confidence = (
            ctx.verdict.confidence if ctx.verdict is not None else min(0.4, ctx.ceiling)
        )
        try:
            ctx.plan = build_plan(
                signal=signal,
                symbol=ctx.report.symbol,
                exchange=ctx.symbol.exchange.value,
                product=(Product.MIS if ctx.timeframe == "intraday" else Product.CNC),
                entry_price=entry,
                stop_loss=stop,
                target_price=target,
                confidence=confidence,
                timeframe=ctx.timeframe,
                invalidation_reason=invalidation,
                data_freshness=ctx.data_freshness,
                as_of=ctx.report.as_of,
                equity_paise=equity,
                available_cash_paise=cash,
                risk_basis_points=risk_bps,
            )
            self._record(
                ctx,
                "size",
                "ok",
                f"{ctx.plan.suggested_quantity} shares, R:R "
                f"{ctx.plan.reward_risk:.2f}, risk per share "
                f"{ctx.plan.risk_per_share}",
            )
        except TradePlanError as exc:
            ctx.vetoes = ctx.vetoes + (f"plan_rejected:{exc}",)
            self._record(ctx, "size", "failed", f"plan rejected: {exc}")

    def _decision(self, ctx: PipelineContext) -> Signal:
        signal = ctx.plan.signal if ctx.plan is not None else Signal.HOLD
        vetoes = f" ({len(ctx.vetoes)} veto(es))" if ctx.vetoes else ""
        self._record(ctx, "decision", "ok", f"{signal.value}{vetoes}")
        return signal

    # -- public -----------------------------------------------------------

    def run(
        self,
        *,
        symbol: Symbol,
        timeframe: str,
        equity_paise: int,
        available_cash_paise: int,
        risk_basis_points: int,
        as_of: datetime | None = None,
    ) -> AnalysisResult:
        if timeframe not in DEBATE_ROUNDS:
            raise PipelineError("Timeframe must be 'intraday' or 'swing'.")
        if isinstance(equity_paise, bool) or equity_paise <= 0:
            raise PipelineError("Equity must be a positive paise amount.")
        if isinstance(available_cash_paise, bool) or available_cash_paise < 0:
            raise PipelineError("Available cash must be nonnegative.")
        if not 0 <= risk_basis_points <= 10_000:
            raise PipelineError("Risk basis points must be within [0, 10000].")

        now = as_of or self.clock.now()
        ctx = PipelineContext(
            symbol=symbol,
            timeframe=timeframe,
            as_of=now.astimezone(UTC),
            interval=(
                BarInterval.DAY_1 if timeframe == "swing" else BarInterval.MINUTE_15
            ),
            lookback_days=365 if timeframe == "swing" else 20,
            started_at=self.clock.now(),
        )

        nodes: tuple[AgentNode[PipelineContext, object], ...] = (
            AgentNode("gather_data", (), self._gather_data),
            AgentNode("news_context", (), self._news_context),
            AgentNode("market_regime", (), self._market_regime),
            AgentNode(
                "fundamental_events",
                ("news_context",),
                self._fundamental_events,
            ),
            AgentNode(
                "evidence_verifier",
                ("news_context", "fundamental_events"),
                self._evidence_verifier,
            ),
            AgentNode("technical", ("gather_data", "news_context"), self._technical),
            AgentNode(
                "bull",
                (
                    "technical",
                    "market_regime",
                    "fundamental_events",
                    "evidence_verifier",
                ),
                self._bull,
            ),
            AgentNode(
                "bear",
                (
                    "technical",
                    "market_regime",
                    "fundamental_events",
                    "evidence_verifier",
                ),
                self._bear,
            ),
            AgentNode("judge", ("bull", "bear"), self._judge),
            AgentNode("risk_team", ("judge",), self._risk_team),
            AgentNode(
                "size",
                ("technical", "judge", "risk_team"),
                lambda context: self._size(
                    context,
                    equity=equity_paise,
                    cash=available_cash_paise,
                    risk_bps=risk_basis_points,
                ),
            ),
            AgentNode("decision", ("size",), self._decision),
            AgentNode("final_gate", ("decision",), self._final_gate),
            AgentNode("confidence", ("final_gate",), self._confidence),
        )
        dag: AgentDAG[PipelineContext, object] = AgentDAG(nodes)
        try:
            result: AgentCycleResult[object] = dag.run("analysis", ctx)
        except AgentDAGError as exc:
            # The DAG wraps node failures to say which node broke. For a caller
            # the useful error is the typed cause (e.g. no market data), so
            # re-raise that with the node context attached.
            cause = exc.__cause__
            if isinstance(cause, PipelineError):
                raise cause from exc
            raise

        assert ctx.report is not None, "technical node must have run"
        if ctx.plan is None and not ctx.vetoes:
            ctx.vetoes = ("no_plan",)

        report = ctx.report
        minimum = MIN_BARS_SWING if timeframe == "swing" else MIN_BARS_INTRADAY
        if report.bar_count < minimum:
            ctx.notes.append(
                f"only {report.bar_count} bars available; {minimum} recommended"
            )

        return AnalysisResult(
            symbol=report.symbol,
            timeframe=timeframe,
            as_of=report.as_of,
            report=report,
            plan=ctx.plan,
            mode=ctx.mode,
            debate_rounds=ctx.debate_rounds_run,
            vendor_daily=ctx.vendor_daily,
            vendor_quotes=ctx.vendor_quotes,
            is_realtime=ctx.is_realtime,
            bull=ctx.bull,
            bear=ctx.bear,
            verdict=ctx.verdict,
            risk=ctx.risk,
            vetoes=ctx.vetoes,
            notes=tuple(ctx.notes),
            input_tokens=None,
            output_tokens=None,
            skipped_nodes=result.skipped,
            # Ordered as the pipeline reads, not as the DAG executed it: the DAG
            # sorts nodes within a layer alphabetically, which put "bear" before
            # "bull" and made the UI read backwards.
            trace=tuple(
                sorted(ctx.trace, key=lambda entry: AGENT_ORDER.index(entry.node))
            ),
            model_routing=(() if self.routing is None else self.routing.routes_used),
            specialists=tuple(
                ctx.specialists[name]
                for name in SPECIALIST_ORDER
                if name in ctx.specialists
            ),
            evidence=ctx.evidence,
            final_gate=ctx.final_gate,
            confidence=ctx.confidence,
        )


__all__ = [
    "AnalysisPipeline",
    "AnalysisResult",
    "DEBATE_ROUNDS",
    "MIN_BARS_INTRADAY",
    "MIN_BARS_SWING",
    "PipelineContext",
    "PipelineError",
]
