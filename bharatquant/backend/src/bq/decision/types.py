"""Typed contracts for the decision layer.

The decision layer follows a strict separation (ADR-018):

* code builds a dense, deterministic state;
* a model (Jev or a configured LLM fallback) answers atomic Choice / Score /
  Noul questions;
* code owns every threshold and every action.

These types are the boundary. A model never returns free text, and a caller never
has to parse prose.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from bq.core import canonical_digest

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_INSTRUCTIONS_CHARS = 500
MAX_OPTION_CHARS = 200
MAX_OPTIONS = 255
MAX_SCORE_LEVELS = 10
MIN_SCORE_LEVELS = 2
DECISION_SOURCES = frozenset({"jev_native", "llm_fallback", "heuristic"})


class DecisionError(ValueError):
    """A decision contract was violated."""


class QuestionError(DecisionError):
    pass


class AnswerError(DecisionError):
    pass


class QuestionType(StrEnum):
    CHOICE = "choice"
    SCORE = "score"
    NOUL = "noul"


def _text(value: object, *, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise QuestionError(f"{label} must be text.")
    text = value.strip()
    if not 1 <= len(text) <= limit:
        raise QuestionError(f"{label} must be between 1 and {limit} characters.")
    if _CONTROL.search(text):
        raise QuestionError(f"{label} must not contain control characters.")
    return text


def _name(value: object) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise QuestionError("A question name must be a short snake_case identifier.")
    return value


def _probability(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnswerError(f"{label} must be a number.")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise AnswerError(f"{label} must be a finite number in [0, 1].")
    return number


@dataclass(frozen=True, slots=True)
class ChoiceQuestion:
    name: str
    instructions: str
    options: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name))
        object.__setattr__(
            self,
            "instructions",
            _text(
                self.instructions, label="instructions", limit=MAX_INSTRUCTIONS_CHARS
            ),
        )
        if not isinstance(self.options, tuple) or not (
            2 <= len(self.options) <= MAX_OPTIONS
        ):
            raise QuestionError("A choice question needs 2 to 255 options.")
        cleaned: list[str] = []
        for option in self.options:
            cleaned.append(_text(option, label="option", limit=MAX_OPTION_CHARS))
        if len(set(cleaned)) != len(cleaned):
            raise QuestionError("Choice options must be unique.")
        object.__setattr__(self, "options", tuple(cleaned))

    @property
    def kind(self) -> QuestionType:
        return QuestionType.CHOICE

    def to_request(self) -> dict[str, object]:
        return {
            "type": self.kind.value,
            "instructions": self.instructions,
            "criteria": {option: None for option in self.options},
        }


@dataclass(frozen=True, slots=True)
class ScoreQuestion:
    name: str
    instructions: str
    levels: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name))
        object.__setattr__(
            self,
            "instructions",
            _text(
                self.instructions, label="instructions", limit=MAX_INSTRUCTIONS_CHARS
            ),
        )
        if not isinstance(self.levels, tuple) or not (
            MIN_SCORE_LEVELS <= len(self.levels) <= MAX_SCORE_LEVELS
        ):
            raise QuestionError("A score question needs 2 to 10 levels.")
        cleaned = [
            _text(level, label="level", limit=MAX_OPTION_CHARS) for level in self.levels
        ]
        if len(set(cleaned)) != len(cleaned):
            raise QuestionError("Score levels must be unique.")
        object.__setattr__(self, "levels", tuple(cleaned))

    @property
    def kind(self) -> QuestionType:
        return QuestionType.SCORE

    def to_request(self) -> dict[str, object]:
        return {
            "type": self.kind.value,
            "instructions": self.instructions,
            "criteria": list(self.levels),
        }


@dataclass(frozen=True, slots=True)
class NoulQuestion:
    name: str
    instructions: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name))
        object.__setattr__(
            self,
            "instructions",
            _text(
                self.instructions, label="instructions", limit=MAX_INSTRUCTIONS_CHARS
            ),
        )

    @property
    def kind(self) -> QuestionType:
        return QuestionType.NOUL

    def to_request(self) -> dict[str, object]:
        return {"type": self.kind.value, "instructions": self.instructions}


Question = ChoiceQuestion | ScoreQuestion | NoulQuestion


@dataclass(frozen=True, slots=True)
class Answer:
    kind: QuestionType
    choice: str | None = None
    score: int | None = None
    noul: float | None = None
    probabilities: tuple[tuple[str, float], ...] = ()
    confidence: float | None = None
    legend: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.kind is QuestionType.CHOICE:
            if not isinstance(self.choice, str) or not self.choice:
                raise AnswerError("A choice answer needs a choice.")
            if self.score is not None or self.noul is not None:
                raise AnswerError("A choice answer cannot carry a score or noul.")
        elif self.kind is QuestionType.SCORE:
            if isinstance(self.score, bool) or not isinstance(self.score, int):
                raise AnswerError("A score answer needs an integer score.")
            if self.score < 0:
                raise AnswerError("A score answer cannot be negative.")
            if self.choice is not None or self.noul is not None:
                raise AnswerError("A score answer cannot carry a choice or noul.")
        else:
            if self.noul is None:
                raise AnswerError("A noul answer needs a probability.")
            _probability(self.noul, label="noul")
            if self.choice is not None or self.score is not None:
                raise AnswerError("A noul answer cannot carry a choice or score.")
            if self.confidence is not None:
                raise AnswerError("Noul answers do not carry confidence.")
        if self.confidence is not None:
            _probability(self.confidence, label="confidence")
        for key, value in self.probabilities:
            if not isinstance(key, str) or not key:
                raise AnswerError("A probability label must be text.")
            _probability(value, label="probability")
        for legend_key, legend_value in self.legend:
            if not isinstance(legend_key, str) or not isinstance(legend_value, str):
                raise AnswerError("A legend must map text to text.")

    def probability(self, label: str) -> float | None:
        for key, value in self.probabilities:
            if key == label:
                return value
        return None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"type": self.kind.value}
        if self.choice is not None:
            payload["choice"] = self.choice
        if self.score is not None:
            payload["score"] = self.score
        if self.noul is not None:
            payload["noul"] = round(self.noul, 6)
        if self.probabilities:
            payload["probabilities"] = {
                key: round(value, 6) for key, value in self.probabilities
            }
        if self.confidence is not None:
            payload["confidence"] = round(self.confidence, 6)
        if self.legend:
            payload["legend"] = dict(self.legend)
        return payload


@dataclass(frozen=True, slots=True)
class JudgmentSet:
    """The typed answers from one battery plus provenance for the run ledger."""

    battery: str
    answers: tuple[tuple[str, Answer], ...]
    model: str
    source: str
    latency_ms: float | None = None
    request_id: str | None = None
    model_mismatch: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.battery:
            raise DecisionError("A judgment set needs a battery name.")
        if not self.model:
            raise DecisionError("A judgment set needs a model identifier.")
        if self.source not in DECISION_SOURCES:
            raise DecisionError("Unknown judgment source.")
        names = [name for name, _ in self.answers]
        if len(set(names)) != len(names):
            raise DecisionError("Judgment names must be unique.")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise DecisionError("Latency cannot be negative.")

    def answer(self, name: str) -> Answer:
        for key, answer in self.answers:
            if key == name:
                return answer
        raise AnswerError(f"The judgment set has no answer named {name!r}.")

    def has(self, name: str) -> bool:
        return any(key == name for key, _ in self.answers)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "battery": self.battery,
            "answers": {name: answer.to_dict() for name, answer in self.answers},
            "model": self.model,
            "source": self.source,
            "model_mismatch": self.model_mismatch,
        }
        if self.latency_ms is not None:
            payload["latency_ms"] = round(self.latency_ms, 3)
        if self.request_id is not None:
            payload["request_id"] = self.request_id
        if self.input_tokens is not None:
            payload["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            payload["output_tokens"] = self.output_tokens
        return payload

    def digest(self) -> str:
        return canonical_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    judgments: JudgmentSet
    provider: str
    model: str
    tier: str
    fallback_used: bool = False
    fallback_from: str | None = None
    state_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.model or not self.tier:
            raise DecisionError("A decision outcome needs provider, model and tier.")
        if self.fallback_used and self.fallback_from is None:
            raise DecisionError("A fallback outcome must name what it fell back from.")

    @property
    def source(self) -> str:
        return self.judgments.source

    def to_dict(self) -> dict[str, object]:
        return {
            "judgments": self.judgments.to_dict(),
            "provider": self.provider,
            "model": self.model,
            "tier": self.tier,
            "source": self.source,
            "fallback_used": self.fallback_used,
            "fallback_from": self.fallback_from,
            "state_hash": self.state_hash,
        }


__all__ = [
    "Answer",
    "AnswerError",
    "ChoiceQuestion",
    "DecisionError",
    "DecisionOutcome",
    "JudgmentSet",
    "NoulQuestion",
    "Question",
    "QuestionError",
    "QuestionType",
    "ScoreQuestion",
]
