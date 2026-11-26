"""Code-owned decision policy.

Thresholds and size multipliers live here, never in a prompt and never in a
model's answer. The policy turns typed judgments into an action plus a size
scale, and it is the only place that knows what a judgment means.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from bq.decision.types import AnswerError, JudgmentSet

EVENT_RISK_BLOCK = 0.8
SOURCE_SCALES = {
    "jev_native": 1.0,
    "llm_fallback": 0.75,
    "heuristic": 0.5,
    "rules_only": 0.0,
}


class ScreenerVerdict(StrEnum):
    RESEARCH = "research"
    SKIP = "skip"


class FinalVerdict(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class ScreenerDecision:
    verdict: ScreenerVerdict
    reason: str
    quality: float
    worth: float
    confidence: float

    @property
    def research(self) -> bool:
        return self.verdict is ScreenerVerdict.RESEARCH


@dataclass(frozen=True, slots=True)
class FinalDecision:
    verdict: FinalVerdict
    execute: bool
    size_scale: float
    reason: str
    conviction: int


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    screener_min_score: float = 0.60
    final_decision_min_prob: float = 0.65
    min_choice_confidence: float = 0.55
    min_noul_probability: float = 0.60
    abstain_score_band: tuple[int, int] = (2, 3)
    llm_fallback_size_scale: float = 0.75
    heuristic_size_scale: float = 0.50

    def __post_init__(self) -> None:
        for value in (
            self.screener_min_score,
            self.final_decision_min_prob,
            self.min_choice_confidence,
            self.min_noul_probability,
            self.llm_fallback_size_scale,
            self.heuristic_size_scale,
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError("Decision thresholds and scales must be in [0, 1].")
        low, high = self.abstain_score_band
        if not 0 <= low <= high:
            raise ValueError("The abstain score band is invalid.")

    def compose_screener(self, judgments: JudgmentSet) -> ScreenerDecision:
        direction = judgments.answer("setup_direction")
        quality = judgments.answer("setup_quality")
        worth = judgments.answer("worth_deep_research")
        event_risk = judgments.answer("event_risk")
        if direction.choice == "stand_aside":
            return ScreenerDecision(
                ScreenerVerdict.SKIP,
                "setup_direction=stand_aside",
                0.0,
                0.0,
                direction.confidence or 0.0,
            )
        if quality.score is None:
            raise AnswerError("The screener battery has no quality score.")
        quality_value = (quality.score + 1) / 5.0
        worth_value = worth.noul if worth.noul is not None else 0.0
        risk_value = event_risk.noul if event_risk.noul is not None else 0.0
        confidence = direction.confidence or 0.0
        reasons: list[str] = []
        if quality_value < self.screener_min_score:
            reasons.append(f"quality {quality_value:.2f}")
        if worth_value < self.screener_min_score:
            reasons.append(f"worth {worth_value:.2f}")
        # Confidence exists on native Jev answers. The deterministic heuristic
        # fixes it at 0.5 and the LLM fallback omits it; neither should be
        # rejected by a native-Jev confidence bar. Size scaling handles quality.
        if (
            judgments.source != "heuristic"
            and confidence is not None
            and confidence > 0.0
            and confidence < self.min_choice_confidence
        ):
            reasons.append(f"confidence {confidence:.2f}")
        if risk_value >= EVENT_RISK_BLOCK:
            reasons.append(f"event_risk {risk_value:.2f}")
        if reasons:
            return ScreenerDecision(
                ScreenerVerdict.SKIP,
                "; ".join(reasons),
                quality_value,
                worth_value,
                confidence,
            )
        return ScreenerDecision(
            ScreenerVerdict.RESEARCH,
            (
                f"quality {quality_value:.2f}, worth {worth_value:.2f}, "
                f"event_risk {risk_value:.2f}"
            ),
            quality_value,
            worth_value,
            confidence,
        )

    def compose_final(self, judgments: JudgmentSet, *, source: str) -> FinalDecision:
        action = judgments.answer("final_action")
        execute = judgments.answer("execute_trade")
        conviction = judgments.answer("conviction_tier")
        if action.choice not in {item.value for item in FinalVerdict}:
            raise AnswerError("The final gate returned an unknown action.")
        verdict = FinalVerdict(action.choice)
        probability = execute.noul if execute.noul is not None else 0.0
        confidence = action.confidence or 0.0
        score = conviction.score if conviction.score is not None else 0
        reasons: list[str] = []
        if verdict is FinalVerdict.HOLD:
            reasons.append("final_action=HOLD")
        if probability < self.final_decision_min_prob:
            reasons.append(f"p_exec {probability:.2f}")
        if (
            source != "heuristic"
            and confidence is not None
            and confidence > 0.0
            and confidence < self.min_choice_confidence
        ):
            reasons.append(f"confidence {confidence:.2f}")
        low, high = self.abstain_score_band
        if low <= score <= high:
            reasons.append(f"conviction {score} in abstain band")
        scale = self.size_scale(source)
        if scale <= 0.0:
            reasons.append("source has no size budget")
        execute_now = not reasons
        strength = min(1.0, 0.5 + 0.125 * (score + 1))
        return FinalDecision(
            verdict=verdict,
            execute=execute_now,
            size_scale=round(scale * strength, 4) if execute_now else 0.0,
            reason="; ".join(reasons) if reasons else f"p_exec {probability:.2f}",
            conviction=score,
        )

    def size_scale(self, source: str) -> float:
        if source == "llm_fallback":
            return self.llm_fallback_size_scale
        if source == "heuristic":
            return self.heuristic_size_scale
        return SOURCE_SCALES.get(source, 0.0)


__all__ = [
    "DecisionPolicy",
    "FinalDecision",
    "FinalVerdict",
    "ScreenerDecision",
    "ScreenerVerdict",
]
