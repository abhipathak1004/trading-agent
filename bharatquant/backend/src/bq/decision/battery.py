"""Atomic question batteries.

One battery per coordinator node. Questions inside a battery are independent, so
one provider call can answer them in parallel. A dependent decision is a separate
coordinator step, never a chained question in the same request (ADR-018).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from bq.decision.types import (
    ChoiceQuestion,
    NoulQuestion,
    Question,
    QuestionError,
    ScoreQuestion,
)

MAX_CLAIMS = 20
MAX_CLAIM_CHARS = 500

SCREENER = "screener"
NEWS_TRIAGE = "news_triage"
EVIDENCE = "evidence"
FINAL_GATE = "final_gate"
BATTERIES = (SCREENER, NEWS_TRIAGE, EVIDENCE, FINAL_GATE)

SETUP_DIRECTIONS = ("long", "short", "stand_aside")
NEWS_CATEGORIES = (
    "INDIA_MARKET",
    "GLOBAL_MARKET",
    "GEOPOLITICAL",
    "COMMODITY",
    "RATES",
    "CORPORATE",
    "REGULATORY",
    "OTHER",
)
FINAL_ACTIONS = ("BUY", "SELL", "HOLD")

_SETUP_QUALITY = (
    "Weak or noisy: no coherent setup, conflicting evidence",
    "Marginal: one dimension supports the idea, the rest are neutral",
    "Acceptable: two dimensions align, no major conflict",
    "Good: three dimensions align with clear evidence",
    "Clean: all four dimensions align across timeframes",
)
_NEWS_HORIZON = (
    "Intraday: relevant for the current session",
    "Swing: relevant over days",
    "Structural: changes the medium-term picture",
)
_CONVICTION = (
    "No edge: the thesis is weak or contradictory",
    "Low: a plausible idea with material caveats",
    "Medium: a supported idea with ordinary uncertainty",
    "High: strong, aligned evidence with a clear invalidation",
    "Very high: exceptional alignment and clean risk",
)


def screener_questions() -> tuple[Question, ...]:
    return (
        ChoiceQuestion(
            name="setup_direction",
            instructions="Which directional setup, if any, does this state describe?",
            options=SETUP_DIRECTIONS,
        ),
        ScoreQuestion(
            name="setup_quality",
            instructions="How clean and aligned is this setup?",
            levels=_SETUP_QUALITY,
        ),
        NoulQuestion(
            name="worth_deep_research",
            instructions=(
                "Is this setup strong enough to justify an expensive multi-agent "
                "research pass?"
            ),
        ),
        NoulQuestion(
            name="event_risk",
            instructions=(
                "Is an unhedged event risk (earnings, corporate action, macro "
                "release) likely to dominate the technical setup?"
            ),
        ),
    )


def news_triage_questions() -> tuple[Question, ...]:
    return (
        NoulQuestion(
            name="material",
            instructions=(
                "Is this news item material to the symbol's near-term price, "
                "rather than routine noise?"
            ),
        ),
        ChoiceQuestion(
            name="category",
            instructions="Which category best describes the item?",
            options=NEWS_CATEGORIES,
        ),
        ScoreQuestion(
            name="horizon",
            instructions="Over what horizon does this item matter?",
            levels=_NEWS_HORIZON,
        ),
    )


def evidence_questions(claims: Sequence[str]) -> tuple[Question, ...]:
    if not 1 <= len(claims) <= MAX_CLAIMS:
        raise QuestionError(f"Evidence batteries need 1 to {MAX_CLAIMS} claims.")
    questions: list[Question] = []
    for index, claim in enumerate(claims, start=1):
        text = claim.strip()
        if not 1 <= len(text) <= MAX_CLAIM_CHARS:
            raise QuestionError("An evidence claim is empty or too long.")
        questions.append(
            NoulQuestion(
                name=f"claim_{index}",
                instructions=f"Is this claim supported by the supplied facts? {text}",
            )
        )
    return tuple(questions)


def final_gate_questions() -> tuple[Question, ...]:
    return (
        ChoiceQuestion(
            name="final_action",
            instructions="What action does the complete thesis support?",
            options=FINAL_ACTIONS,
        ),
        NoulQuestion(
            name="execute_trade",
            instructions=(
                "Should this trade be executed now, after accounting for the "
                "thesis, regime and portfolio context?"
            ),
        ),
        ScoreQuestion(
            name="conviction_tier",
            instructions="How strong is the conviction behind this action?",
            levels=_CONVICTION,
        ),
    )


def decision_schema(questions: Sequence[Question]) -> dict[str, object]:
    """A strict JSON Schema for the same questions, used by the LLM fallback."""
    properties: dict[str, object] = {}
    for question in questions:
        if isinstance(question, ChoiceQuestion):
            properties[question.name] = {
                "type": "string",
                "enum": list(question.options),
            }
        elif isinstance(question, ScoreQuestion):
            properties[question.name] = {
                "type": "integer",
                "minimum": 0,
                "maximum": len(question.levels) - 1,
            }
        else:
            properties[question.name] = {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            }
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }


def decision_prompt(state: Mapping[str, object], questions: Sequence[Question]) -> str:
    """The fallback prompt: state plus the question definitions, no prose state."""
    payload = {
        "state": state,
        "questions": {question.name: question.to_request() for question in questions},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def questions_for(battery: str, *, claims: Sequence[str] = ()) -> tuple[Question, ...]:
    if battery == SCREENER:
        return screener_questions()
    if battery == NEWS_TRIAGE:
        return news_triage_questions()
    if battery == EVIDENCE:
        return evidence_questions(claims)
    if battery == FINAL_GATE:
        return final_gate_questions()
    raise QuestionError(f"Unknown battery {battery!r}.")


__all__ = [
    "BATTERIES",
    "decision_prompt",
    "decision_schema",
    "EVIDENCE",
    "FINAL_ACTIONS",
    "FINAL_GATE",
    "NEWS_CATEGORIES",
    "NEWS_TRIAGE",
    "SCREENER",
    "SETUP_DIRECTIONS",
    "evidence_questions",
    "final_gate_questions",
    "news_triage_questions",
    "questions_for",
    "screener_questions",
]
