"""Structured-output fallback for decision batteries.

When Jev is unavailable, the same atomic questions are sent to a configured text
model as JSON constrained by a generated schema. The parsed result goes through
the same validation as a native answer, so callers see one contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from bq.llm_service import CompletionOutcome
from bq.model_output import ModelOutputError, validate_against_schema

MAX_REPAIR_ATTEMPTS = 1


class StructuredCompleter(Protocol):
    def __call__(
        self,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str,
        timeout: float | None,
    ) -> CompletionOutcome: ...


def _schema_text(schema: Mapping[str, object]) -> str:
    return json.dumps(
        schema,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def complete_structured(
    completer: StructuredCompleter,
    *,
    prompt: str,
    system: str,
    schema: Mapping[str, object],
    task_id: str,
    task_version: str = "v1",
    timeout: float | None = None,
    repair_attempts: int = MAX_REPAIR_ATTEMPTS,
) -> tuple[object, CompletionOutcome]:
    """Ask a text model for schema-conforming JSON, with one repair attempt."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A structured prompt is required.")
    if not 0 <= repair_attempts <= 2:
        raise ValueError("repair_attempts must be between 0 and 2.")
    schema_text = _schema_text(schema)
    instruction = (
        f"{system}\n"
        "Return only one JSON object that matches this JSON Schema exactly. "
        "Do not include prose, markdown fences or extra keys.\n"
        f"{schema_text}"
    )
    outcome = completer(
        prompt=prompt,
        system=instruction,
        task_id=task_id,
        task_version=task_version,
        timeout=timeout,
    )
    last_error: ModelOutputError | None = None
    for attempt in range(repair_attempts + 1):
        try:
            return validate_against_schema(schema_text, outcome.text), outcome
        except ModelOutputError as exc:
            last_error = exc
            if attempt >= repair_attempts:
                break
            outcome = completer(
                prompt=(
                    f"{prompt}\n\nYour previous answer was rejected: {exc}. "
                    "Answer again with only the JSON object."
                ),
                system=instruction,
                task_id=task_id,
                task_version=task_version,
                timeout=timeout,
            )
    assert last_error is not None
    raise last_error


__all__ = ["StructuredCompleter", "complete_structured"]
