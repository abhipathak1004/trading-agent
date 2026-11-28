"""Deterministic fallback judgments.

Adapted from the reference implementation's ``HeuristicJudge`` idea (MIT): when
no model answers, code still produces a complete, reproducible judgment set. The
confidences are fixed at 0.5 because these rules are **not calibrated**; policy
compensates by scaling size down. Nothing here uses randomness or the wall clock.
"""

from __future__ import annotations

from collections.abc import Sequence

from bq.decision.battery import (
    EVIDENCE,
    FINAL_GATE,
    NEWS_TRIAGE,
    SCREENER,
    final_gate_questions,
    news_triage_questions,
    screener_questions,
)
from bq.decision.state import DecisionState
from bq.decision.types import Answer, JudgmentSet, QuestionError, QuestionType

HEURISTIC_CONFIDENCE = 0.5
HEURISTIC_MODEL = "heuristic"


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _score(value: float, levels: int) -> int:
    if levels < 2:
        raise QuestionError("A score needs at least two levels.")
    return max(0, min(levels - 1, int(round(value))))


class HeuristicJudge:
    """Code rules that answer the same batteries as the model."""

    def judge(
        self,
        *,
        state: DecisionState,
        battery: str,
        claims: Sequence[str] = (),
    ) -> JudgmentSet:
        if battery == SCREENER:
            questions = screener_questions()
            answers = self._screener(state)
        elif battery == NEWS_TRIAGE:
            questions = news_triage_questions()
            answers = self._news_triage()
        elif battery == EVIDENCE:
            raise QuestionError("The heuristic cannot verify evidence claims.")
        elif battery == FINAL_GATE:
            questions = final_gate_questions()
            answers = self._final_gate(state)
        else:
            raise QuestionError(f"Unknown battery {battery!r}.")
        return JudgmentSet(
            battery=battery,
            answers=tuple(zip((question.name for question in questions), answers)),
            model=HEURISTIC_MODEL,
            source="heuristic",
            latency_ms=0.0,
        )

    def _screener(self, state: DecisionState) -> tuple[Answer, ...]:
        composite = state.composite or 0.0
        trend = state.trend or 0.0
        direction = self._direction(composite, trend)
        quality = self._quality(state)
        worth = _clip(0.5 + composite)
        event_risk = self._event_risk(state)
        return (
            Answer(
                kind=QuestionType.CHOICE,
                choice=direction,
                confidence=HEURISTIC_CONFIDENCE,
            ),
            Answer(
                kind=QuestionType.SCORE,
                score=quality,
                confidence=HEURISTIC_CONFIDENCE,
            ),
            Answer(kind=QuestionType.NOUL, noul=worth),
            Answer(kind=QuestionType.NOUL, noul=event_risk),
        )

    def _news_triage(self) -> tuple[Answer, ...]:
        return (
            Answer(kind=QuestionType.NOUL, noul=0.5),
            Answer(
                kind=QuestionType.CHOICE,
                choice="OTHER",
                confidence=HEURISTIC_CONFIDENCE,
            ),
            Answer(
                kind=QuestionType.SCORE,
                score=1,
                confidence=HEURISTIC_CONFIDENCE,
            ),
        )

    def _final_gate(self, state: DecisionState) -> tuple[Answer, ...]:
        composite = state.composite or 0.0
        trend = state.trend or 0.0
        if composite > 0.1 and trend > 0.0:
            action = "BUY"
            probability = _clip(0.5 + 0.8 * composite)
        elif composite < -0.2 and trend < 0.0:
            action = "SELL"
            probability = _clip(0.5 + 0.8 * abs(composite))
        else:
            action = "HOLD"
            probability = 0.0
        conviction = self._conviction(state)
        return (
            Answer(
                kind=QuestionType.CHOICE,
                choice=action,
                confidence=HEURISTIC_CONFIDENCE,
            ),
            Answer(kind=QuestionType.NOUL, noul=probability),
            Answer(
                kind=QuestionType.SCORE,
                score=conviction,
                confidence=HEURISTIC_CONFIDENCE,
            ),
        )

    @staticmethod
    def _direction(composite: float, trend: float) -> str:
        if composite > 0.15 and trend > 0.0:
            return "long"
        if composite < -0.15 and trend < 0.0:
            return "short"
        return "stand_aside"

    @staticmethod
    def _quality(state: DecisionState) -> int:
        composite = abs(state.composite or 0.0)
        adx = state.adx_14 or 0.0
        volume = state.volume_relative or 1.0
        raw = composite * 4.0
        if adx >= 25.0:
            raw += 1.0
        if volume >= 1.2:
            raw += 1.0
        return _score(raw, 5)

    @staticmethod
    def _event_risk(state: DecisionState) -> float:
        if state.last_close <= 0:
            return 1.0
        atr_pct = (state.atr_14 or 0.0) / state.last_close
        volatility = max(0.0, -(state.volatility or 0.0))
        return _clip(atr_pct / 0.05 + 0.5 * volatility)

    @staticmethod
    def _conviction(state: DecisionState) -> int:
        adx = state.adx_14 or 0.0
        volume = state.volume_relative or 1.0
        composite = abs(state.composite or 0.0)
        raw = composite * 3.0
        if adx >= 25.0:
            raw += 0.8
        if volume >= 1.2:
            raw += 0.5
        return _score(raw, 5)


__all__ = ["HEURISTIC_CONFIDENCE", "HEURISTIC_MODEL", "HeuristicJudge"]
