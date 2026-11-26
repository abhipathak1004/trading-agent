"""TypeSafe Jev client.

One endpoint, one request, atomic questions. The client is deliberately narrow:
it validates the payload, enforces a hard deadline, and returns a typed judgment
set. It never decides to fall back on its own — the router owns that ladder.

Adapted from the hardening in the reference implementation
``buberlo/jev-trader`` (MIT): pin the model, record the returned version and
request id, measure latency, and never retry past the decision deadline.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal

import httpx

from bq.decision.types import (
    Answer,
    AnswerError,
    ChoiceQuestion,
    JudgmentSet,
    NoulQuestion,
    Question,
    QuestionError,
    QuestionType,
    ScoreQuestion,
)
from bq.llm_result import parse_usage
from bq.llm_retry import TRANSIENT_STATUS, backoff_delay

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_STATE_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_ATTEMPT_SECONDS = 0.05
TRANSIENT = TRANSIENT_STATUS | {529}


class JevError(ValueError):
    """A Jev call failed or returned something unusable."""


class JevNotConfigured(JevError):
    """No Jev key or model is configured."""


class JevUnavailable(JevError):
    """Jev could not answer inside its budget."""


class _Retryable(JevError):
    pass


def _as_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise JevError(f"Jev returned an invalid {label}.")
    return value


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevError(f"Jev returned a non-numeric {label}.")
    number = float(value)
    if not math.isfinite(number):
        raise JevError(f"Jev returned a non-finite {label}.")
    return number


def _probability(value: object, label: str) -> float:
    number = _finite(value, label)
    if not 0.0 <= number <= 1.0:
        raise JevError(f"Jev returned {label} outside [0, 1].")
    return number


def _pairs(value: object, label: str) -> tuple[tuple[str, float], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise JevError(f"Jev returned an invalid {label}.")
    pairs: list[tuple[str, float]] = []
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise JevError(f"Jev returned an invalid {label} label.")
        pairs.append((key, _probability(item, label)))
    return tuple(pairs)


def _legend(value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise JevError("Jev returned an invalid legend.")
    pairs: list[tuple[str, str]] = []
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise JevError("Jev returned an invalid legend entry.")
        pairs.append((key, item))
    return tuple(pairs)


class JevClient:
    """Synchronous Jev client with injectable transport and clock."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        endpoint: str = JEV_ENDPOINT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parts = endpoint.split("://", 1)
        if len(parts) != 2 or parts[0] != "https" or not parts[1]:
            raise JevError("The Jev endpoint must be an HTTPS URL.")
        self.endpoint = endpoint
        self.transport = transport
        self._clock = clock
        self._sleep = sleep

    def decide(
        self,
        *,
        model: str,
        api_key: str,
        state: Mapping[str, object],
        questions: Sequence[Question],
        timeout: float,
        max_attempts: int = 1,
        battery: str = "decision",
    ) -> JudgmentSet:
        if not isinstance(model, str) or not model.strip():
            raise JevNotConfigured("No Jev model is configured.")
        if not isinstance(api_key, str) or not api_key.strip():
            raise JevNotConfigured("No TypeSafe Jev key is configured.")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise JevError("A decision timeout is required.")
        budget = float(timeout)
        if budget <= 0:
            raise JevError("The decision timeout must be positive.")
        if not 1 <= max_attempts <= 3:
            raise JevError("max_attempts must be between 1 and 3.")
        if not questions:
            raise QuestionError("A Jev request needs at least one question.")
        names = [question.name for question in questions]
        if len(set(names)) != len(names):
            raise QuestionError("Question names must be unique.")

        try:
            state_text = json.dumps(
                state, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise JevError("Decision state is not valid JSON.") from exc
        if len(state_text.encode("utf-8")) > MAX_STATE_BYTES:
            raise JevError("Decision state exceeds the Jev byte limit.")
        body: dict[str, object] = {
            "model": model,
            "state": json.loads(state_text),
            "questions": {
                question.name: question.to_request() for question in questions
            },
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "BharatQuant/0.1 (personal research)",
        }

        started = self._clock()
        deadline = started + budget
        payload: dict[str, object] | None = None
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            remaining = deadline - self._clock()
            if remaining <= MIN_ATTEMPT_SECONDS:
                break
            try:
                payload = self._post(
                    body=body,
                    headers=headers,
                    timeout=max(MIN_ATTEMPT_SECONDS, remaining * 0.8),
                )
                last_error = None
                break
            except _Retryable as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
                delay = backoff_delay(attempt)
                if self._clock() + delay + MIN_ATTEMPT_SECONDS >= deadline:
                    break
                self._sleep(delay)
        if payload is None:
            detail = "no attempt fitted the deadline"
            if last_error is not None:
                detail = type(last_error).__name__
            raise JevUnavailable(f"Jev did not answer inside its budget ({detail}).")

        latency_ms = (self._clock() - started) * 1000.0
        return self._parse(
            payload,
            questions=questions,
            requested_model=model,
            battery=battery,
            latency_ms=latency_ms,
        )

    def _post(
        self, *, body: dict[str, object], headers: dict[str, str], timeout: float
    ) -> dict[str, object]:
        try:
            with httpx.Client(timeout=timeout, transport=self.transport) as client:
                response = client.post(self.endpoint, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise _Retryable("Jev request timed out.") from exc
        except httpx.HTTPError as exc:
            raise _Retryable(f"Jev transport failed: {type(exc).__name__}.") from exc
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise JevError("Jev response exceeded the size limit.")
        if response.status_code >= 400:
            # Never echo the body: it can contain account or key fragments.
            if response.status_code in TRANSIENT:
                raise _Retryable(f"Jev returned HTTP {response.status_code}.")
            raise JevError(f"Jev returned HTTP {response.status_code}.")
        try:
            return _as_dict(response.json(), "response body")
        except JevError:
            raise
        except ValueError as exc:
            raise JevError("Jev response was not valid JSON.") from exc

    def _parse(
        self,
        payload: Mapping[str, object],
        *,
        questions: Sequence[Question],
        requested_model: str,
        battery: str,
        latency_ms: float,
    ) -> JudgmentSet:
        returned_model = payload.get("model")
        if not isinstance(returned_model, str) or not returned_model:
            raise JevError("Jev response is missing its model identifier.")
        raw_answers = _as_dict(payload.get("answers"), "answers")
        expected = {question.name for question in questions}
        if set(raw_answers) != expected:
            raise JevError("Jev answers do not match the requested questions.")
        answers: list[tuple[str, Answer]] = []
        for question in questions:
            answers.append(
                (
                    question.name,
                    self._parse_answer(
                        question,
                        _as_dict(raw_answers[question.name], "answer"),
                    ),
                )
            )
        request_id = payload.get("request_id")
        if request_id is None:
            request_id = payload.get("id")
        usage = parse_usage(payload.get("usage"))
        return JudgmentSet(
            battery=battery,
            answers=tuple(answers),
            model=returned_model,
            source="jev_native",
            latency_ms=latency_ms,
            request_id=request_id if isinstance(request_id, str) else None,
            model_mismatch=returned_model != requested_model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    def _parse_answer(self, question: Question, raw: Mapping[str, object]) -> Answer:
        declared = raw.get("type")
        if declared is not None and declared != question.kind.value:
            raise JevError("Jev returned the wrong answer type.")
        probabilities = _pairs(raw.get("probabilities"), "probabilities")
        legend = _legend(raw.get("legend"))
        confidence = raw.get("confidence")
        parsed_confidence = (
            None if confidence is None else _probability(confidence, "confidence")
        )
        if isinstance(question, ChoiceQuestion):
            choice = raw.get("choice")
            if not isinstance(choice, str) or choice not in question.options:
                raise JevError("Jev returned an unknown choice.")
            return Answer(
                kind=QuestionType.CHOICE,
                choice=choice,
                probabilities=probabilities,
                confidence=parsed_confidence,
                legend=legend,
            )
        if isinstance(question, ScoreQuestion):
            score = _finite(raw.get("score"), "score")
            rounded = int(score)
            if not math.isclose(score, rounded, abs_tol=1e-9):
                raise JevError("Jev returned a non-integral score.")
            if not 0 <= rounded < len(question.levels):
                raise JevError("Jev returned a score outside the rubric.")
            return Answer(
                kind=QuestionType.SCORE,
                score=rounded,
                probabilities=probabilities,
                confidence=parsed_confidence,
                legend=legend,
            )
        if isinstance(question, NoulQuestion):
            return Answer(
                kind=QuestionType.NOUL,
                noul=_probability(raw.get("noul"), "noul"),
                probabilities=probabilities,
                legend=legend,
            )
        raise QuestionError("Unknown question type.")


def parse_decisions(
    payload: Mapping[str, object], questions: Sequence[Question]
) -> tuple[tuple[str, Answer], ...]:
    """Parse the flat JSON produced by the LLM fallback into typed answers."""
    expected = {question.name for question in questions}
    if set(payload) != expected:
        raise JevError("Decision answers do not match the requested questions.")
    parsed: list[tuple[str, Answer]] = []
    for question in questions:
        value = payload[question.name]
        if isinstance(question, ChoiceQuestion):
            if not isinstance(value, str) or value not in question.options:
                raise AnswerError("A fallback choice is not one of the options.")
            parsed.append(
                (question.name, Answer(kind=QuestionType.CHOICE, choice=value))
            )
            continue
        if isinstance(question, ScoreQuestion):
            if isinstance(value, Decimal):
                if value != value.to_integral_value():
                    raise AnswerError("A fallback score must be an integer.")
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int):
                raise AnswerError("A fallback score must be an integer.")
            if not 0 <= value < len(question.levels):
                raise AnswerError("A fallback score is outside the rubric.")
            parsed.append((question.name, Answer(kind=QuestionType.SCORE, score=value)))
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise AnswerError("A fallback noul must be a number.")
        number = float(value)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise AnswerError("A fallback noul must be in [0, 1].")
        parsed.append((question.name, Answer(kind=QuestionType.NOUL, noul=number)))
    return tuple(parsed)


__all__ = [
    "JEV_ENDPOINT",
    "JevClient",
    "JevError",
    "JevNotConfigured",
    "JevUnavailable",
    "parse_decisions",
]
