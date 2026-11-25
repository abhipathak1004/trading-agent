"""The risk analyst: advisory only, never a veto.

Upstream's risk debate is the final word. Here it is not. The only thing that can
block a trade in this application is the deterministic gate in
``bq/risk/policy.py`` (plan §3, principle 11), so this agent's output feeds the
plan's ``invalidation_reason`` and an early-warning list — it cannot approve a
trade and it cannot stop one.

That distinction matters more than it looks: if a model could veto, a prompt
injection or a bad model day would silently halt trading. If a model could
approve, it could override the risk limits. Neither is acceptable, so this stage
is bounded to description.
"""

from __future__ import annotations

from dataclasses import dataclass

from bq.analysis.debate import (
    DebateUnavailable,
    ModelCaller,
    _bounded,
    _extract_json,
)
from bq.analysis.prompts import RISK_SYSTEM, RISK_TEMPLATE
from bq.analysis.technical import TechnicalReport

MAX_WARNING_CHARS = 200
MAX_WARNINGS = 5
MAX_INVALIDATION_CHARS = 400


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    invalidation_reason: str
    early_warnings: tuple[str, ...]
    volatility_flag: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "invalidation_reason": self.invalidation_reason,
            "early_warnings": list(self.early_warnings),
            "volatility_flag": self.volatility_flag,
            "advisory_only": True,
        }


class RiskAnalyst:
    """Describes what would invalidate a setup. Does not decide anything."""

    def __init__(self, caller: ModelCaller) -> None:
        self._caller = caller

    def assess(self, report: TechnicalReport) -> RiskAssessment:
        prompt = RISK_TEMPLATE.format(
            symbol=report.symbol,
            timeframe=report.timeframe,
            as_of=report.as_of.isoformat(),
            facts="\n".join(report.facts()),
        )
        try:
            outcome = self._caller.complete(
                prompt=prompt,
                system=RISK_SYSTEM,
                task_id="analysis.risk",
                task_version="v1",
            )
        except Exception as exc:
            raise DebateUnavailable(
                f"The risk-model call failed: {type(exc).__name__}."
            ) from exc
        text = getattr(outcome, "text", None)
        payload = _extract_json(text if isinstance(text, str) else "")

        warnings_raw = payload.get("early_warnings")
        warnings: list[str] = []
        if isinstance(warnings_raw, list):
            for item in warnings_raw[:MAX_WARNINGS]:
                if isinstance(item, str) and item.strip():
                    warnings.append(item.strip()[:MAX_WARNING_CHARS])

        flag = payload.get("volatility_flag")
        return RiskAssessment(
            invalidation_reason=_bounded(
                payload.get("invalidation_reason"),
                limit=MAX_INVALIDATION_CHARS,
                label="invalidation_reason",
            ),
            early_warnings=tuple(warnings),
            volatility_flag=bool(flag) if isinstance(flag, bool) else False,
        )


def deterministic_invalidation(report: TechnicalReport) -> str:
    """A code-derived invalidation reason, used when the model is unavailable.

    This is what makes ``RULES_ONLY`` an honest label: the plan still states what
    would invalidate it, from the numbers rather than from a provider.
    """
    swings = report.indicators.swings
    if swings is not None and swings.support is not None:
        return (
            f"Closes below the swing support at {swings.support:.2f}, "
            f"invalidating the structure."
        )
    if report.trend is not None and report.trend.value < 0:
        return "Trend dimension is negative; a long has no trend support."
    return (
        f"Close moves more than one ATR from {report.last_close} against the position."
    )


__all__ = [
    "MAX_INVALIDATION_CHARS",
    "MAX_WARNINGS",
    "RiskAnalyst",
    "RiskAssessment",
    "deterministic_invalidation",
]
