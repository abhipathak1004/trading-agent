"""The Jev-preferred final gate after the deterministic plan.

The gate answers three typed questions about the complete thesis: the action,
whether to execute it, and the conviction tier. Code then applies
``DecisionPolicy``. The gate can downgrade a plan; it cannot create one, and it
never places an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bq.analysis.decision import TradePlan
from bq.analysis.technical import TechnicalReport
from bq.decision.battery import FINAL_GATE
from bq.decision.policy import DecisionPolicy, FinalDecision
from bq.decision.state import DecisionState, build_decision_state
from bq.decision.types import DecisionOutcome


class DecisionRouterLike(Protocol):
    def decide(
        self,
        agent_id: str,
        *,
        state: DecisionState,
        battery: str,
        task_id: str = "",
        claims: tuple[str, ...] = (),
    ) -> DecisionOutcome: ...


@dataclass(frozen=True, slots=True)
class FinalGateResult:
    outcome: DecisionOutcome
    decision: FinalDecision
    state: DecisionState

    @property
    def execute(self) -> bool:
        return self.decision.execute

    def to_dict(self) -> dict[str, object]:
        judgments = self.outcome.judgments
        return {
            "action": self.decision.verdict.value,
            "execute": self.decision.execute,
            "size_scale": self.decision.size_scale,
            "reason": self.decision.reason,
            "conviction": self.decision.conviction,
            "source": self.outcome.source,
            "provider": self.outcome.provider,
            "model": self.outcome.model,
            "tier": self.outcome.tier,
            "fallback_used": self.outcome.fallback_used,
            "fallback_from": self.outcome.fallback_from,
            "latency_ms": judgments.latency_ms,
            "request_id": judgments.request_id,
            "model_mismatch": judgments.model_mismatch,
            "state_hash": self.outcome.state_hash,
            "judgments": judgments.to_dict(),
        }


class FinalGate:
    def __init__(self, router: DecisionRouterLike, policy: DecisionPolicy) -> None:
        self.router = router
        self.policy = policy

    def evaluate(
        self,
        report: TechnicalReport,
        plan: TradePlan,
        *,
        data_freshness: str,
    ) -> FinalGateResult:
        state = build_decision_state(report, data_freshness=data_freshness, plan=plan)
        outcome = self.router.decide(
            "final_decision_gate",
            state=state,
            battery=FINAL_GATE,
            task_id="analysis.final_gate",
        )
        decision = self.policy.compose_final(outcome.judgments, source=outcome.source)
        return FinalGateResult(outcome=outcome, decision=decision, state=state)


__all__ = ["DecisionRouterLike", "FinalGate", "FinalGateResult"]
