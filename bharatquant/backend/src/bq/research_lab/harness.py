"""The bounded research loop: plan -> deterministic tools -> evidence gate.

Mirrors Algorithm 1 of arXiv:2611.24845 as far as this repository can honestly
implement it:

1. the policy chooses a tool or proposes a final answer;
2. tool results are admitted into E_t through the ledger — the harness never
   admits model output;
3. a proposed answer is checked against E_t, scoped to the run each claim names;
4. a failure triggers bounded recovery (revision), then removal of the failing
   figures and a re-check, and only then a fail-closed fallback.

Two hard bounds make the loop terminate: an iteration cap and a no-progress
detector. A third bounds the recovery: `max_revision_retries`.

The model turn and the tools are injected, so the loop is testable with no
network and no model.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from bq.research_lab.evidence import (
    EvidenceClaim,
    EvidenceLedger,
    decisions,
)

MAX_ITERATIONS: Final = 20
MAX_RETRIES: Final = 5
MAX_TEXT_CHARS: Final = 20_000
MAX_STALLS: Final = 2

FALLBACK_TEXT: Final = (
    "The report could not be verified against admitted evidence, so no figures "
    "are reported. Re-run with a tool that produces evidence for this request."
)

ModelTurn = Callable[[str, tuple[Mapping[str, object], ...]], Mapping[str, object]]
ToolCall = Callable[[Mapping[str, object]], Mapping[str, object]]
Emit = Callable[[str, Mapping[str, object]], None]


class HarnessError(ValueError):
    """The harness cannot continue with this decision."""


@dataclass(frozen=True, slots=True)
class ResearchTool:
    name: str
    summary: str
    run: ToolCall


@dataclass(frozen=True, slots=True)
class HarnessOutcome:
    status: str
    text: str
    validation: Mapping[str, object]
    iterations: int
    tools_used: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "text": self.text,
            "validation": dict(self.validation),
            "iterations": self.iterations,
            "tools_used": list(self.tools_used),
        }


def _evidence_numbers(value: object) -> dict[str, float]:
    """The numbers a model may cite, so its claims can be exact rather than guessed."""
    if not isinstance(value, Mapping):
        return {}
    numbers: dict[str, float] = {}
    for key, raw in value.items():
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            number = float(raw)
            if number == number and number not in (float("inf"), float("-inf")):
                numbers[str(key)] = number
    return numbers


def _keys(value: object) -> list[str]:
    if isinstance(value, Mapping):
        return sorted(str(key) for key in value)
    return []


def _refs(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _claims(raw: object) -> list[EvidenceClaim]:
    if not isinstance(raw, list):
        return []
    claims: list[EvidenceClaim] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise HarnessError("A claim must be an object.")
        try:
            claims.append(
                EvidenceClaim(
                    run_id=str(item["run_id"]),
                    metric_key=str(item["metric_key"]),
                    value=float(str(item["value"])),
                    unit=str(item.get("unit", "ratio")),
                    time_basis=str(item.get("time_basis", "point")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HarnessError("A claim is malformed.") from exc
    return claims


def _admit(
    ledger: EvidenceLedger, tool_name: str, result: Mapping[str, object], run_id: str
) -> str | None:
    evidence = result.get("evidence")
    units = result.get("units")
    if not isinstance(evidence, Mapping) or not evidence:
        return None
    if not isinstance(units, Mapping):
        raise HarnessError("A tool that reports evidence must report its units.")
    source_run = str(result.get("run_id", run_id))
    metrics: dict[str, float] = {}
    unit_map: dict[str, tuple[str, str]] = {}
    for key, value in evidence.items():
        unit_entry = units.get(key)
        if not isinstance(unit_entry, (list, tuple)) or len(unit_entry) != 2:
            raise HarnessError("An evidence unit must be (unit, time_basis).")
        metrics[str(key)] = float(str(value))
        unit_map[str(key)] = (str(unit_entry[0]), str(unit_entry[1]))
    ledger.admit(
        source_run,
        tool_name,
        metrics,
        unit_map,
        source_ref=str(result.get("source_ref", "")),
    )
    return source_run


class ResearchHarness:
    def __init__(
        self,
        *,
        ledger: EvidenceLedger,
        tools: Sequence[ResearchTool] = (),
        max_iterations: int = 12,
        max_recovery_retries: int = 2,
        max_revision_retries: int = 2,
    ) -> None:
        if not 1 <= max_iterations <= MAX_ITERATIONS:
            raise HarnessError("The iteration budget is outside its bound.")
        if not 0 <= max_revision_retries <= MAX_RETRIES:
            raise HarnessError("The revision budget is outside its bound.")
        self.ledger = ledger
        self.tools = {tool.name: tool for tool in tools}
        self.max_iterations = max_iterations
        self.max_recovery_retries = max_recovery_retries
        self.max_revision_retries = max_revision_retries

    async def run(
        self,
        *,
        prompt: str,
        run_id: str,
        emit: Emit,
        model_turn: ModelTurn,
    ) -> HarnessOutcome:
        observations: list[dict[str, object]] = []
        used: list[str] = []
        last_signature: str | None = None
        stalls = 0

        for iteration in range(1, self.max_iterations + 1):
            emit("step", {"name": "plan", "iteration": iteration})
            decision = await asyncio.to_thread(model_turn, prompt, tuple(observations))
            if not isinstance(decision, Mapping):
                raise HarnessError("The model decision must be an object.")
            action = str(decision.get("action", ""))

            if action == "tool":
                name = str(decision.get("tool", ""))
                tool = self.tools.get(name)
                if tool is None:
                    raise HarnessError(f"The harness has no tool named {name!r}.")
                arguments = decision.get("arguments")
                if not isinstance(arguments, Mapping):
                    arguments = {}
                signature = json.dumps(
                    {"tool": name, "arguments": arguments}, sort_keys=True
                )
                stalls = stalls + 1 if signature == last_signature else 0
                last_signature = signature
                if stalls >= MAX_STALLS:
                    emit("error", {"detail": "no new observation; stopping"})
                    return HarnessOutcome(
                        "blocked",
                        "The run repeated a call without new evidence and was stopped.",
                        {"passed": False, "reason": "no_progress"},
                        iteration,
                        tuple(used),
                    )
                emit("tool_call", {"tool": name, "arguments": dict(arguments)})
                result = await asyncio.to_thread(tool.run, dict(arguments))
                if not isinstance(result, Mapping):
                    raise HarnessError(f"Tool {name!r} returned a non-object.")
                source_run = _admit(self.ledger, name, result, run_id)
                if source_run is not None:
                    emit(
                        "provenance",
                        {
                            "run_id": source_run,
                            "metrics": _keys(result.get("evidence")),
                        },
                    )
                used.append(name)
                observations.append(
                    {
                        "step": iteration,
                        "tool": name,
                        "summary": str(result.get("summary", ""))[:2000],
                        "run_id": source_run or run_id,
                        "artifact_refs": _refs(result.get("artifact_refs")),
                        "evidence": _evidence_numbers(result.get("evidence")),
                    }
                )
                continue

            if action == "final":
                return await self._gate(
                    decision,
                    prompt=prompt,
                    run_id=run_id,
                    emit=emit,
                    observations=observations,
                    model_turn=model_turn,
                    iterations=iteration,
                    used=tuple(used),
                )

            raise HarnessError("A decision must be a tool call or a final answer.")

        emit("error", {"detail": "iteration budget exhausted"})
        return HarnessOutcome(
            "blocked",
            "The iteration budget was exhausted before a verified report was produced.",
            {"passed": False, "reason": "iteration_budget"},
            self.max_iterations,
            tuple(used),
        )

    async def _gate(
        self,
        decision: Mapping[str, object],
        *,
        prompt: str,
        run_id: str,
        emit: Emit,
        observations: list[dict[str, object]],
        model_turn: ModelTurn,
        iterations: int,
        used: tuple[str, ...],
    ) -> HarnessOutcome:
        text = str(decision.get("text", ""))[:MAX_TEXT_CHARS]
        claims = _claims(decision.get("claims"))
        result: dict[str, object] = self.ledger.validate_report(text, claims)
        emit(
            "evidence_verdict",
            {
                "passed": result["passed"],
                "accepted": result["accepted"],
                "rejected": result["rejected"],
                "attempt": 1,
            },
        )
        if result["passed"]:
            return HarnessOutcome("completed", text, result, iterations, used)

        failures = [item for item in decisions(result) if not item.get("accepted")]
        attempt = 1
        while attempt <= self.max_revision_retries and failures:
            attempt += 1
            emit("step", {"name": "revision", "attempt": attempt})
            revision_history = tuple(observations) + (
                {"revision_request": {"failures": failures}},
            )
            revised = await asyncio.to_thread(model_turn, prompt, revision_history)
            if (
                not isinstance(revised, Mapping)
                or str(revised.get("action")) != "final"
            ):
                break
            text = str(revised.get("text", text))[:MAX_TEXT_CHARS]
            claims = _claims(revised.get("claims"))
            result = self.ledger.validate_report(text, claims)
            emit(
                "evidence_verdict",
                {
                    "passed": result["passed"],
                    "accepted": result["accepted"],
                    "rejected": result["rejected"],
                    "attempt": attempt,
                },
            )
            if result["passed"]:
                return HarnessOutcome("completed", text, result, iterations, used)
            failures = [item for item in decisions(result) if not item.get("accepted")]

        surviving = [
            claim
            for claim, detail in zip(claims, decisions(result), strict=False)
            if detail.get("accepted")
        ]
        if surviving:
            reduced = self.ledger.validate_report(text, surviving)
            emit(
                "evidence_verdict",
                {
                    "passed": reduced["passed"],
                    "accepted": reduced["accepted"],
                    "rejected": reduced["rejected"],
                    "reduced": True,
                },
            )
            if reduced["passed"]:
                return HarnessOutcome("completed", text, reduced, iterations, used)
        emit("error", {"detail": "unverified report refused"})
        return HarnessOutcome("blocked", FALLBACK_TEXT, result, iterations, used)


__all__ = [
    "FALLBACK_TEXT",
    "HarnessError",
    "HarnessOutcome",
    "MAX_ITERATIONS",
    "ResearchHarness",
    "ResearchTool",
]
