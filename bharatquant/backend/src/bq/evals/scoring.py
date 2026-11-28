"""Bridge to the repo-root ``evals/score_promptfoo_results.py`` scorer.

The scorer is deliberately dependency-free and is usable from the command line,
so it is not a package module. We import it by file path so the API computes the
exact same numbers as ``python3 evals/score_promptfoo_results.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, cast


class ScoringError(RuntimeError):
    """The scorer module or a results file could not be loaded."""


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One graded test case, flattened for the failure-inspector drawer."""

    description: str
    model: str
    category: str
    passed: bool
    input: str
    raw_output: str
    confidence: float | None
    latency_ms: float | None
    failure_reasons: tuple[str, ...] = field(default=())


@lru_cache(maxsize=4)
def load_scorer(scorer_path: Path) -> ModuleType:
    """Import the standalone scorer module from ``scorer_path`` (cached)."""
    path = scorer_path.resolve()
    if not path.is_file():
        raise ScoringError(f"Scorer module is missing: {path.name}.")
    module_name = "bq_eval_scorer"
    existing = sys.modules.get(module_name)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ScoringError("Could not create an import spec for the scorer.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    for required in ("load_result_items", "analyze_results", "export_csv"):
        if not hasattr(module, required):
            raise ScoringError(f"Scorer module is missing {required}().")
    return module


def _load_payload(results_path: Path) -> Any:
    if not results_path.is_file():
        raise ScoringError(f"Results file is missing: {results_path.name}.")
    with results_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def analyze_results_file(
    scorer_path: Path, results_path: Path
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(summaries, sorted_categories)`` for a Promptfoo results file."""
    scorer = load_scorer(scorer_path)
    try:
        items = scorer.load_result_items(_load_payload(results_path))
        summaries, categories = scorer.analyze_results(items)
    except (ValueError, KeyError, TypeError) as exc:
        raise ScoringError(f"Could not score results file: {exc}") from exc
    return (
        cast(list[dict[str, Any]], summaries),
        cast(list[str], categories),
    )


def export_scorecard_csv(
    scorer_path: Path,
    summaries: list[dict[str, Any]],
    categories: list[str],
    out_path: Path,
) -> None:
    """Write the per-model scorecard CSV next to the run's results file."""
    scorer = load_scorer(scorer_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scorer.export_csv(summaries, categories, out_path)


def _failure_reasons(item: dict[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    grading = item.get("gradingResult") or {}
    for component in grading.get("componentResults") or []:
        if component.get("pass"):
            continue
        assertion = component.get("assertion") or {}
        reason = (
            component.get("reason") or assertion.get("metric") or "assertion failed"
        )
        reasons.append(str(reason))
    return tuple(reasons)


def _case_confidence(item: dict[str, Any]) -> float | None:
    raw = (item.get("response") or {}).get("output")
    if raw is None:
        raw = item.get("output", "")
    text = raw if isinstance(raw, str) else json.dumps(raw)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("confidence")
    if isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0:
        return float(value)
    return None


def load_cases(scorer_path: Path, results_path: Path) -> list[EvalCase]:
    """Flatten every graded case in a results file for the inspector drawer."""
    scorer = load_scorer(scorer_path)
    try:
        items = scorer.load_result_items(_load_payload(results_path))
    except (ValueError, KeyError, TypeError) as exc:
        raise ScoringError(f"Could not read results file: {exc}") from exc

    cases: list[EvalCase] = []
    for item in items:
        test_case = item.get("testCase") or {}
        vars_dict = item.get("vars") or test_case.get("vars") or {}
        description = (
            test_case.get("description")
            or item.get("description")
            or (vars_dict.get("__description") if isinstance(vars_dict, dict) else None)
            or ""
        )
        prompt_input = ""
        if isinstance(vars_dict, dict):
            prompt_input = str(vars_dict.get("user_input") or "")
        latency = item.get("latencyMs")
        cases.append(
            EvalCase(
                description=str(description),
                model=scorer.get_provider_name(item),
                category=scorer.get_category(item),
                passed=bool(item.get("success")),
                input=prompt_input,
                raw_output=scorer.get_raw_output(item),
                confidence=_case_confidence(item),
                latency_ms=float(latency)
                if isinstance(latency, (int, float))
                else None,
                failure_reasons=_failure_reasons(item),
            )
        )
    return cases
