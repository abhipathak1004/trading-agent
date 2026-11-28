"""Wire the harness to the real model router.

The harness takes a ``model_turn`` callable so it can be tested without a model.
This module supplies the production one: it renders the task and the observations
into a prompt, asks the routed model for a JSON decision, and parses it.

The model only ever *proposes*. Every number it reports is checked against the
evidence ledger afterwards, and numbers may only reach that ledger through a
deterministic tool.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from bq.dirty_json import parse
from bq.model_router import ModelRouter
from bq.research_lab.harness import HarnessError, ModelTurn

SYSTEM = """You are the BharatQuant Research Lab synthesizer.

You work in rounds. Each reply is ONE JSON object and nothing else.

To call a tool:
{"action": "tool", "tool": "<name>", "arguments": { ... }}

To answer:
{"action": "final", "text": "<the answer>", "claims": [
  {"run_id": "<run the number came from>", "metric_key": "<tool metric>",
   "value": <number>, "unit": "ratio|pct|bp|inr|paise|count|bars",
   "time_basis": "point|per_day|per_bar|annualized"}
]}

Rules that are enforced in code, not by you:
- every numeric claim must cite the run that produced it; a value borrowed from
  another run is rejected;
- unit and time_basis must match what the tool reported;
- you cannot introduce a number that no tool returned.
Report only figures a tool produced. If you have no evidence, say so in text and
return an empty claims list."""


def render(prompt: str, observations: tuple[Mapping[str, object], ...]) -> str:
    lines = ["task:", prompt.strip(), "", "observations:"]
    if not observations:
        lines.append("(none yet)")
    for item in observations:
        if "revision_request" in item:
            lines.append(
                "revision needed: "
                + json.dumps(item["revision_request"], sort_keys=True)
            )
            continue
        lines.append(
            "- step {step} {tool}: {summary}".format(
                step=item.get("step", "?"),
                tool=item.get("tool", "?"),
                summary=item.get("summary", ""),
            )
        )
    return "\n".join(lines)


def router_model_turn(
    router: ModelRouter, *, agent_id: str, system: str = SYSTEM
) -> ModelTurn:
    def turn(
        prompt: str, observations: tuple[Mapping[str, object], ...]
    ) -> Mapping[str, object]:
        outcome = router.complete(
            agent_id,
            prompt=render(prompt, observations),
            system=system,
            task_id="research_lab.harness",
        )
        try:
            decision = parse(outcome.text)
        except Exception as exc:  # noqa: BLE001 - the parser is third-party
            raise HarnessError("The model did not return JSON.") from exc
        if not isinstance(decision, dict):
            raise HarnessError("The model decision must be a JSON object.")
        return decision

    return turn


__all__ = ["SYSTEM", "render", "router_model_turn"]
