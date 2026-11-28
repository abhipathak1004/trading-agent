"""The engine's `LLMClient`, implemented over BharatQuant's model router.

Upstream each persona reached a vendor directly: ``make_llm()`` read an api-key
environment variable, chose a langchain client from a model id, and called it.
That is exactly what this app does not allow — a model must come from
Settings, a key must come from the settings store, and every call must land in
the operational log. So this module implements the engine's one-method protocol
(``complete(system, user) -> str``) over ``ModelRouter`` and nothing else.

Two consequences worth stating:

* **The persona is an agent, not a model.** Each persona is registered in
  Settings as its own agent id (``aihf_buffett``, …), so the tier — and any
  per-agent model override — is chosen in the UI. ``RouterLLM`` is constructed
  per persona with that id; nothing here names a model.
* **The answer is validated before the engine sees it.** ``complete_structured``
  enforces ``{signal, confidence, reasoning}`` and repairs once. The engine's own
  parser then reads the JSON. A malformed answer becomes the engine's normal
  "abstain" path rather than a crash, which is the behaviour upstream documents
  for a persona that cannot form a view.

What this deliberately does *not* do: cache. The engine owns its prompt cache
(``engine/llm/cache.py``, keyed by filing-stable snapshot hash), and a second
cache here would spend the same filing twice under two keys.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from bq.aihf.engine.llm.protocol import LLMCallError, LLMParseError
from bq.llm_service import CompletionOutcome
from bq.model_output import ModelOutputError
from bq.model_router import ModelRouter, ModelRoutingError
from bq.structured_output import complete_structured

# The JSON a persona must return. Confidence is 0–100 because that is the range
# the engine's own parser accepts (`signals/llm_agent.py::_parse`).
SIGNAL_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["signal", "confidence", "reasoning"],
    "properties": {
        "signal": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        "reasoning": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
}

# Persona id → the agent id it routes as. Registered in Settings, so the tier
# and any per-agent model are configuration (see `bq/agent_catalogue.py`).
PERSONA_AGENTS: dict[str, str] = {
    "buffett": "aihf_buffett",
    "munger": "aihf_munger",
    "graham": "aihf_graham",
    "lynch": "aihf_lynch",
    "druckenmiller": "aihf_druckenmiller",
}


def agent_id_for(persona: str) -> str:
    """The Settings agent id a persona routes as.

    An unregistered persona name would silently fall through `route_for` to the
    reasoning tier; this refuses instead, so a new persona must be registered
    (and therefore visible in the UI) rather than quietly priced at the default.
    """
    agent = PERSONA_AGENTS.get(persona)
    if agent is None:
        raise ValueError(
            f"persona {persona!r} has no registered agent id; add it to "
            "PERSONA_AGENTS and to the settings catalogue"
        )
    return agent


class RouterLLM:
    """`LLMClient` for one persona, routed through Settings.

    The engine reads ``.model`` for the session record and calls ``.complete()``
    once per (ticker, session). ``user`` is the rendered point-in-time snapshot;
    ``system`` is the persona's prompt, which the owner can edit in the Agent Lab
    and which this class does not touch.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        persona: str,
        task_version: str = "v1",
        metrics: Any | None = None,
        metrics_prompt: str | None = None,
    ) -> None:
        self._router = router
        self._persona = persona
        self._agent_id = agent_id_for(persona)
        self._task_version = task_version
        self._metrics = metrics
        self._metrics_prompt = metrics_prompt
        try:
            self.model = router.resolve(self._agent_id).model
        except ModelRoutingError:
            # Not configured yet: the engine records the id it *would* have used
            # and every call will fail into an abstain, which is visible in the
            # Agent Lab rather than crashing a backtest on its first ticker.
            self.model = "unconfigured"

    @property
    def persona(self) -> str:
        return self._persona

    @property
    def agent_id(self) -> str:
        return self._agent_id

    # ------------------------------------------------------------------
    # LLMClient protocol
    # ------------------------------------------------------------------

    def complete(self, system: str, user: str) -> str:
        """Return the persona's assessment as JSON text.

        Raises ``LLMCallError`` on a transport failure; returns a parseable JSON
        string when the model answered in-schema. The engine's own parser turns
        either into a Signal or an abstention — it never sees an exception from a
        mis-typed answer, only from a failed call.
        """
        try:
            parsed, outcome = complete_structured(
                self._completer,
                prompt=self._prompt(user),
                system=system,
                schema=SIGNAL_SCHEMA,
                task_id=f"aihf.persona.{self._persona}",
                task_version=self._task_version,
            )
        except ModelOutputError as exc:
            raise LLMParseError(
                f"{self._persona}: the model did not return the required JSON"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - the engine expects a typed error
            raise LLMCallError(f"{self._persona}: {type(exc).__name__}") from exc

        if not isinstance(parsed, Mapping):
            raise LLMParseError(
                f"{self._persona}: the model returned {type(parsed).__name__}"
            )
        # Carry the model id the call actually used, so the session record names
        # what answered rather than what was configured.
        self.model = outcome.model
        return json.dumps(dict(parsed), sort_keys=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _completer(
        self,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str,
        timeout: float | None,
    ) -> CompletionOutcome:
        return self._router.complete(
            self._agent_id,
            prompt=prompt,
            system=system,
            task_id=task_id,
            task_version=task_version,
            timeout=timeout,
        )

    def _prompt(self, user: str) -> str:
        """The rendered snapshot, plus the metric block when the persona has one.

        Upstream the persona prompt already embeds whatever facts the snapshot
        renders. ``metrics_prompt`` exists for callers that want the deterministic
        ratio table alongside it (the engine never passes one today); it stays
        additive and off by default rather than changing the persona's inputs.
        """
        if not self._metrics_prompt:
            return user
        return f"{user}\n\n{self._metrics_prompt}"


def build_persona_clients(
    router: ModelRouter,
    personas: list[str],
    *,
    task_version: str = "v1",
) -> dict[str, RouterLLM]:
    """One client per persona, so each routes to its own settings entry."""
    return {
        persona: RouterLLM(router, persona=persona, task_version=task_version)
        for persona in personas
    }


def persona_llm(
    router: ModelRouter,
    persona: str,
    *,
    task_version: str = "v1",
) -> RouterLLM:
    return RouterLLM(router, persona=persona, task_version=task_version)


__all__ = [
    "PERSONA_AGENTS",
    "RouterLLM",
    "SIGNAL_SCHEMA",
    "agent_id_for",
    "build_persona_clients",
    "persona_llm",
]
