"""Super Agent orchestration for an owner-supplied strategy.

The coordinator is code. It resolves the requested universe, runs the existing
per-symbol agent DAG (technical, news context, debate, judge, risk, sizer,
decision), then asks the reasoning tier for a portfolio ranking and applies a
deterministic cash/position gate. The Super Agent can downgrade a plan; it can
never create a BUY that the deterministic pipeline did not produce, and nothing
here places an order.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, cast

from bq.agent_prompts import (
    SUPER_AGENT_SYSTEM,
    SUPER_AGENT_TEMPLATE,
    PromptSource,
    resolve_prompt,
)
from bq.analysis.batch import BatchError, parse_symbol_list
from bq.analysis.decision import TradePlan
from bq.analysis.pipeline import AnalysisResult
from bq.core import Clock, SystemClock
from bq.data.symbols import Symbol, SymbolError
from bq.data.symbols import resolve as resolve_symbol
from bq.llm_service import CompletionOutcome
from bq.memory import Playbook, PlaybookSource
from bq.structured_output import complete_structured

MAX_STRATEGY_CHARS = 2_000
MAX_SYMBOLS = 8
MAX_POSITIONS = 10
MAX_NOTES_CHARS = 1_200

SUPER_AGENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "strategy_summary": {"type": "string", "maxLength": MAX_NOTES_CHARS},
        "ranked_actions": {
            "type": "array",
            "maxItems": MAX_SYMBOLS,
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "action": {"type": "string", "enum": ["BUY", "HOLD", "AVOID"]},
                    "rank": {"type": "integer", "minimum": 1},
                    "size_scale": {"type": "number", "minimum": 0, "maximum": 1},
                    "rationale": {"type": "string", "maxLength": 600},
                },
                "required": ["symbol", "action", "rank", "size_scale", "rationale"],
                "additionalProperties": False,
            },
        },
        "portfolio_notes": {"type": "string", "maxLength": MAX_NOTES_CHARS},
        "risk_notes": {"type": "string", "maxLength": MAX_NOTES_CHARS},
    },
    "required": [
        "strategy_summary",
        "ranked_actions",
        "portfolio_notes",
        "risk_notes",
    ],
    "additionalProperties": False,
}

ACTIONS = frozenset({"BUY", "HOLD", "AVOID"})


class StrategyError(ValueError):
    """The strategy request could not be executed."""


class PipelineLike(Protocol):
    def run(
        self,
        *,
        symbol: Symbol,
        timeframe: str,
        equity_paise: int,
        available_cash_paise: int,
        risk_basis_points: int,
    ) -> AnalysisResult: ...


class RouterLike(Protocol):
    def complete(
        self,
        agent_id: str,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str = "v1",
        timeout: float | None = None,
    ) -> CompletionOutcome: ...


@dataclass(frozen=True, slots=True)
class _RouterCompleter:
    router: RouterLike
    agent_id: str

    def __call__(
        self,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str,
        timeout: float | None,
    ) -> CompletionOutcome:
        return self.router.complete(
            self.agent_id,
            prompt=prompt,
            system=system,
            task_id=task_id,
            task_version=task_version,
            timeout=timeout,
        )


@dataclass(frozen=True, slots=True)
class RankedAction:
    symbol: str
    action: str
    rank: int
    size_scale: float
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "rank": self.rank,
            "size_scale": self.size_scale,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    strategy_summary: str
    ranked_actions: tuple[RankedAction, ...]
    portfolio_notes: str
    risk_notes: str
    source: str
    model: str | None
    fallback_used: bool
    fallback_from: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_summary": self.strategy_summary,
            "ranked_actions": [action.to_dict() for action in self.ranked_actions],
            "portfolio_notes": self.portfolio_notes,
            "risk_notes": self.risk_notes,
            "source": self.source,
            "model": self.model,
            "fallback_used": self.fallback_used,
            "fallback_from": self.fallback_from,
        }


@dataclass(frozen=True, slots=True)
class StrategySpec:
    strategy: str
    timeframe: str
    symbols: tuple[str, ...]
    max_positions: int
    equity_paise: int
    available_cash_paise: int
    risk_basis_points: int
    playbook: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "timeframe": self.timeframe,
            "symbols": list(self.symbols),
            "max_positions": self.max_positions,
            "equity_paise": self.equity_paise,
            "available_cash_paise": self.available_cash_paise,
            "risk_basis_points": self.risk_basis_points,
            "playbook": self.playbook,
            "execution": "PAPER_RESEARCH",
        }


@dataclass(frozen=True, slots=True)
class SymbolOutcome:
    requested: str
    symbol: str
    ok: bool
    error: str | None
    result: AnalysisResult | None

    def to_dict(self) -> dict[str, object]:
        if not self.ok or self.result is None:
            return {
                "requested": self.requested,
                "symbol": self.symbol,
                "ok": False,
                "error": self.error or "Analysis failed.",
            }
        analysis = self.result
        plan = analysis.plan
        return {
            "requested": self.requested,
            "symbol": self.symbol,
            "ok": True,
            "error": None,
            "signal": "HOLD" if plan is None else plan.signal.value,
            "composite": analysis.report.composite,
            "mode": analysis.mode,
            "data_freshness": (
                "realtime_kite" if analysis.is_realtime else "delayed_yfinance"
            ),
            "plan": None if plan is None else plan.to_dict(),
            "agents": [entry.to_dict() for entry in analysis.trace],
            "facts": list(analysis.report.facts())[:8],
            "vetoes": list(analysis.vetoes),
            "notes": list(analysis.notes),
            "model_routing": [use.to_dict() for use in analysis.model_routing],
            "final_gate": (
                None if analysis.final_gate is None else analysis.final_gate.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class AcceptedPlan:
    symbol: str
    rank: int
    action: str
    size_scale: float
    rationale: str
    plan: TradePlan

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "rank": self.rank,
            "action": self.action,
            "size_scale": self.size_scale,
            "rationale": self.rationale,
            "notional_paise": self.plan.notional_paise().value,
            "plan": self.plan.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RejectedPlan:
    symbol: str
    action: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class StrategyRun:
    strategy: str
    timeframe: str
    as_of: datetime
    spec: StrategySpec
    symbols: tuple[SymbolOutcome, ...]
    decision: StrategyDecision
    accepted: tuple[AcceptedPlan, ...]
    rejected: tuple[RejectedPlan, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "timeframe": self.timeframe,
            "as_of": self.as_of.isoformat(),
            "spec": self.spec.to_dict(),
            "symbols": [outcome.to_dict() for outcome in self.symbols],
            "decision": self.decision.to_dict(),
            "accepted": [plan.to_dict() for plan in self.accepted],
            "rejected": [plan.to_dict() for plan in self.rejected],
            "notes": list(self.notes),
        }


def _bounded(value: str, limit: int) -> str:
    text = value.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _candidate_payload(outcome: SymbolOutcome) -> dict[str, object]:
    analysis = outcome.result
    if analysis is None:
        raise StrategyError("A failed symbol has no payload.")
    plan = analysis.plan
    return {
        "symbol": outcome.symbol,
        "signal": "HOLD" if plan is None else plan.signal.value,
        "composite": analysis.report.composite,
        "mode": analysis.mode,
        "data_freshness": (
            "realtime_kite" if analysis.is_realtime else "delayed_yfinance"
        ),
        "plan": None if plan is None else plan.to_dict(),
        "facts": list(analysis.report.facts())[:6],
        "vetoes": list(analysis.vetoes),
        "risk": None if analysis.risk is None else analysis.risk.to_dict(),
        "verdict": None if analysis.verdict is None else analysis.verdict.to_dict(),
    }


def _rank_key(outcome: SymbolOutcome) -> tuple[int, float, float]:
    analysis = outcome.result
    if analysis is None:
        return (0, -2.0, 0.0)
    plan = analysis.plan
    signal_rank = 1 if plan is not None and plan.signal.value == "BUY" else 0
    composite = analysis.report.composite
    confidence = plan.confidence if plan is not None else 0.0
    return (signal_rank, composite if composite is not None else -2.0, confidence)


class SuperAgent:
    def __init__(
        self,
        *,
        pipeline_factory: Callable[[], PipelineLike],
        router_factory: Callable[[], RouterLike],
        prompts: PromptSource,
        clock: Clock | None = None,
        max_symbols: int = MAX_SYMBOLS,
        playbooks: PlaybookSource | None = None,
    ) -> None:
        self._pipeline_factory = pipeline_factory
        self._router_factory = router_factory
        self._prompts = prompts
        self._clock = clock or SystemClock()
        self._max_symbols = max(1, min(max_symbols, MAX_SYMBOLS))
        self._playbooks = playbooks

    def run(
        self,
        *,
        strategy: str,
        symbols: str,
        timeframe: str,
        equity_paise: int,
        available_cash_paise: int,
        risk_basis_points: int,
        max_positions: int,
        playbook_id: str | None = None,
    ) -> StrategyRun:
        if not isinstance(strategy, str) or not strategy.strip():
            raise StrategyError("A strategy brief is required.")
        text = strategy.strip()
        if len(text) > MAX_STRATEGY_CHARS:
            raise StrategyError("The strategy brief is too long.")
        if timeframe not in {"intraday", "swing"}:
            raise StrategyError("Timeframe must be intraday or swing.")
        if not 1 <= max_positions <= MAX_POSITIONS:
            raise StrategyError("Max positions is out of range.")
        playbook: Playbook | None = None
        if playbook_id is not None:
            if self._playbooks is None:
                raise StrategyError("No playbooks are configured.")
            try:
                playbook = self._playbooks.get(playbook_id)
            except Exception as exc:
                raise StrategyError("Unknown playbook.") from exc
        try:
            requested = parse_symbol_list(symbols)
        except BatchError as exc:
            raise StrategyError(str(exc)) from exc
        if not requested:
            raise StrategyError("At least one symbol is required.")
        if len(requested) > self._max_symbols:
            raise StrategyError(
                f"At most {self._max_symbols} symbols can be analysed in one strategy run."
            )

        outcomes: list[SymbolOutcome] = []
        for item in requested:
            try:
                symbol = resolve_symbol(item)
            except SymbolError as exc:
                outcomes.append(SymbolOutcome(item, item, False, str(exc), None))
                continue
            if not symbol.tradeable:
                outcomes.append(
                    SymbolOutcome(
                        item,
                        symbol.canonical,
                        False,
                        "Indices are context, not trade candidates.",
                        None,
                    )
                )
                continue
            try:
                pipeline = self._pipeline_factory()
                result = pipeline.run(
                    symbol=symbol,
                    timeframe=timeframe,
                    equity_paise=equity_paise,
                    available_cash_paise=available_cash_paise,
                    risk_basis_points=risk_basis_points,
                )
            except Exception as exc:  # one symbol must not kill the strategy
                outcomes.append(
                    SymbolOutcome(
                        item,
                        symbol.canonical,
                        False,
                        str(exc) or type(exc).__name__,
                        None,
                    )
                )
                continue
            outcomes.append(SymbolOutcome(item, symbol.canonical, True, None, result))

        analyzed = [
            outcome for outcome in outcomes if outcome.ok and outcome.result is not None
        ]
        if not analyzed:
            failures = "; ".join(outcome.error or "unknown" for outcome in outcomes)
            raise StrategyError(f"No symbol produced an analysis ({failures}).")

        accepted, rejected = self._gate(
            analyzed,
            max_positions=max_positions,
            available_cash_paise=available_cash_paise,
        )
        decision = self._synthesize(
            strategy=text,
            timeframe=timeframe,
            analyzed=analyzed,
            max_positions=max_positions,
            equity_paise=equity_paise,
            available_cash_paise=available_cash_paise,
            risk_basis_points=risk_basis_points,
            playbook=playbook,
        )
        accepted, rejected = self._apply_decision(decision, accepted, rejected)
        spec = StrategySpec(
            strategy=text,
            timeframe=timeframe,
            symbols=tuple(outcome.symbol for outcome in analyzed),
            max_positions=max_positions,
            equity_paise=equity_paise,
            available_cash_paise=available_cash_paise,
            risk_basis_points=risk_basis_points,
            playbook=None if playbook is None else playbook.playbook_id,
        )
        return StrategyRun(
            strategy=text,
            timeframe=timeframe,
            as_of=self._clock.now(),
            spec=spec,
            symbols=tuple(outcomes),
            decision=decision,
            accepted=tuple(accepted),
            rejected=tuple(rejected),
            notes=(
                "Paper research only: no orders are placed by the Super Agent.",
                "A BUY can only come from a deterministic per-symbol plan; the "
                "Super Agent may downgrade it.",
            ),
        )

    def _gate(
        self,
        analyzed: Sequence[SymbolOutcome],
        *,
        max_positions: int,
        available_cash_paise: int,
    ) -> tuple[list[AcceptedPlan], list[RejectedPlan]]:
        accepted: list[AcceptedPlan] = []
        rejected: list[RejectedPlan] = []
        committed = 0
        for outcome in sorted(analyzed, key=_rank_key, reverse=True):
            analysis = outcome.result
            assert analysis is not None
            plan = analysis.plan
            if plan is None:
                rejected.append(
                    RejectedPlan(
                        outcome.symbol,
                        "HOLD",
                        "No deterministic long plan: "
                        + (", ".join(analysis.vetoes) or "HOLD"),
                    )
                )
                continue
            if plan.signal.value != "BUY":
                rejected.append(
                    RejectedPlan(
                        outcome.symbol,
                        plan.signal.value,
                        f"Deterministic signal is {plan.signal.value}.",
                    )
                )
                continue
            gate = analysis.final_gate
            if gate is not None and not gate.decision.execute:
                rejected.append(
                    RejectedPlan(
                        outcome.symbol,
                        "BUY",
                        f"Final gate: {gate.decision.reason}",
                    )
                )
                continue
            if len(accepted) >= max_positions:
                rejected.append(
                    RejectedPlan(
                        outcome.symbol,
                        "BUY",
                        "Maximum position count reached.",
                    )
                )
                continue
            notional = plan.notional_paise().value
            if committed + notional > available_cash_paise:
                rejected.append(
                    RejectedPlan(
                        outcome.symbol,
                        "BUY",
                        "Insufficient cash for the plan's sized quantity.",
                    )
                )
                continue
            committed += notional
            accepted.append(
                AcceptedPlan(
                    symbol=outcome.symbol,
                    rank=len(accepted) + 1,
                    action="BUY",
                    size_scale=1.0,
                    rationale="Deterministic BUY plan.",
                    plan=plan,
                )
            )
        return accepted, rejected

    def _synthesize(
        self,
        *,
        strategy: str,
        timeframe: str,
        analyzed: Sequence[SymbolOutcome],
        max_positions: int,
        equity_paise: int,
        available_cash_paise: int,
        risk_basis_points: int,
        playbook: Playbook | None = None,
    ) -> StrategyDecision:
        candidates = [_candidate_payload(outcome) for outcome in analyzed]
        constraints = {
            "max_positions": max_positions,
            "equity_paise": equity_paise,
            "available_cash_paise": available_cash_paise,
            "risk_basis_points": risk_basis_points,
            "execution": "PAPER_RESEARCH",
            "rules": [
                "long-only",
                "no new BUY without a deterministic BUY plan",
                "respect the position cap and cash",
                "no orders are placed",
            ],
            "playbook": (
                None
                if playbook is None
                else {
                    "id": playbook.playbook_id,
                    "name": playbook.name,
                    "version": playbook.version,
                    "guidance": playbook.content,
                }
            ),
        }
        router: RouterLike | None = None
        try:
            system, template = resolve_prompt(
                self._prompts,
                "super_agent_cio",
                SUPER_AGENT_SYSTEM,
                SUPER_AGENT_TEMPLATE,
            )
            prompt = template.format(
                strategy=strategy,
                timeframe=timeframe,
                constraints=json.dumps(constraints, sort_keys=True),
                candidates=json.dumps(candidates, sort_keys=True),
            )
            router = self._router_factory()
            parsed, outcome = complete_structured(
                _RouterCompleter(router, "super_agent_cio"),
                prompt=prompt,
                system=system,
                schema=SUPER_AGENT_SCHEMA,
                task_id="strategy.super_agent",
            )
            decision = self._parse_decision(
                parsed,
                analyzed,
                model=outcome.model,
                fallback_used=self._fallback_used(router),
                fallback_from=self._fallback_from(router),
            )
            if decision is not None:
                return decision
        except Exception:  # noqa: BLE001 - a model failure degrades to rules
            pass
        return self._rules_decision(strategy, analyzed)

    def _parse_decision(
        self,
        parsed: object,
        analyzed: Sequence[SymbolOutcome],
        *,
        model: str,
        fallback_used: bool,
        fallback_from: str | None,
    ) -> StrategyDecision | None:
        if not isinstance(parsed, Mapping):
            return None
        summary = parsed.get("strategy_summary")
        portfolio = parsed.get("portfolio_notes")
        risk = parsed.get("risk_notes")
        raw_actions = parsed.get("ranked_actions")
        if (
            not isinstance(summary, str)
            or not isinstance(portfolio, str)
            or not isinstance(risk, str)
            or not isinstance(raw_actions, list)
        ):
            return None
        symbols = {outcome.symbol for outcome in analyzed}
        actions: list[RankedAction] = []
        seen: set[str] = set()
        for item in raw_actions:
            if not isinstance(item, Mapping):
                continue
            symbol = item.get("symbol")
            action = item.get("action")
            rank = item.get("rank")
            scale = item.get("size_scale")
            rationale = item.get("rationale")
            if (
                not isinstance(symbol, str)
                or symbol not in symbols
                or symbol in seen
                or action not in ACTIONS
                or isinstance(rank, bool)
                or not isinstance(rank, int)
                or rank < 1
                or not isinstance(scale, (int, float, Decimal))
                or isinstance(scale, bool)
                or not 0.0 <= float(scale) <= 1.0
                or not isinstance(rationale, str)
            ):
                continue
            seen.add(symbol)
            actions.append(
                RankedAction(
                    symbol=symbol,
                    action=str(action),
                    rank=rank,
                    size_scale=float(scale),
                    rationale=_bounded(rationale, 600),
                )
            )
        if not actions:
            return None
        actions.sort(key=lambda item: item.rank)
        for outcome in analyzed:
            if outcome.symbol not in seen:
                actions.append(
                    RankedAction(
                        symbol=outcome.symbol,
                        action="HOLD",
                        rank=len(actions) + 1,
                        size_scale=1.0,
                        rationale="Not ranked by the Super Agent; held by default.",
                    )
                )
        return StrategyDecision(
            strategy_summary=_bounded(summary, MAX_NOTES_CHARS),
            ranked_actions=tuple(actions),
            portfolio_notes=_bounded(portfolio, MAX_NOTES_CHARS),
            risk_notes=_bounded(risk, MAX_NOTES_CHARS),
            source="reasoning",
            model=model,
            fallback_used=fallback_used,
            fallback_from=fallback_from,
        )

    def _rules_decision(
        self, strategy: str, analyzed: Sequence[SymbolOutcome]
    ) -> StrategyDecision:
        actions: list[RankedAction] = []
        for rank, outcome in enumerate(
            sorted(analyzed, key=_rank_key, reverse=True), start=1
        ):
            analysis = outcome.result
            assert analysis is not None
            plan = analysis.plan
            action = (
                "BUY" if plan is not None and plan.signal.value == "BUY" else "HOLD"
            )
            composite = analysis.report.composite
            actions.append(
                RankedAction(
                    symbol=outcome.symbol,
                    action=action,
                    rank=rank,
                    size_scale=1.0,
                    rationale=(
                        f"Deterministic composite {composite if composite is not None else 'n/a'}; "
                        f"plan {action}."
                    ),
                )
            )
        return StrategyDecision(
            strategy_summary=_bounded(
                f"Rule-based synthesis for: {strategy}", MAX_NOTES_CHARS
            ),
            ranked_actions=tuple(actions),
            portfolio_notes=(
                "No reasoning model answered, so candidates are ranked by the "
                "deterministic composite and plan signal."
            ),
            risk_notes=(
                "Long-only. Plan quantities are unchanged; the cash and position "
                "gate is deterministic. No orders are placed."
            ),
            source="rules",
            model=None,
            fallback_used=True,
            fallback_from="reasoning",
        )

    @staticmethod
    def _apply_decision(
        decision: StrategyDecision,
        accepted: list[AcceptedPlan],
        rejected: list[RejectedPlan],
    ) -> tuple[list[AcceptedPlan], list[RejectedPlan]]:
        by_symbol = {action.symbol: action for action in decision.ranked_actions}
        final: list[AcceptedPlan] = []
        for plan in accepted:
            action = by_symbol.get(plan.symbol)
            if action is None or action.action != "BUY" or action.size_scale <= 0.0:
                reason = (
                    "Super Agent did not rank it as BUY."
                    if action is None or action.action != "BUY"
                    else "Super Agent sized it at zero."
                )
                rejected.append(RejectedPlan(plan.symbol, "BUY", reason))
                continue
            final.append(
                AcceptedPlan(
                    symbol=plan.symbol,
                    rank=action.rank,
                    action="BUY",
                    size_scale=action.size_scale,
                    rationale=action.rationale,
                    plan=plan.plan,
                )
            )
        final.sort(key=lambda item: item.rank)
        return final, rejected

    @staticmethod
    def _fallback_used(router: RouterLike) -> bool:
        routes = getattr(router, "routes_used", ())
        if not routes:
            return False
        return bool(routes[-1].fallback_used)

    @staticmethod
    def _fallback_from(router: RouterLike) -> str | None:
        routes = getattr(router, "routes_used", ())
        if not routes:
            return None
        value = routes[-1].fallback_from
        return cast(str | None, value)


__all__ = [
    "AcceptedPlan",
    "MAX_POSITIONS",
    "MAX_STRATEGY_CHARS",
    "MAX_SYMBOLS",
    "PipelineLike",
    "RankedAction",
    "RejectedPlan",
    "RouterLike",
    "StrategyDecision",
    "StrategyError",
    "StrategyRun",
    "StrategySpec",
    "SuperAgent",
    "SymbolOutcome",
]
