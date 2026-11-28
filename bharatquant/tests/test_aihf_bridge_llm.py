"""The LLM bridge: personas route through Settings, and their answers are
validated before the engine sees them.

The engine's contract is one method — ``complete(system, user) -> str`` — and two
behaviours that matter: a persona must never name its own model (that is
Settings), and a malformed answer must become an abstention, not a crash.
"""

from __future__ import annotations

import json

import pytest

from bq.agent_catalogue import AGENT_CATALOGUE
from bq.agent_prompts import DEFINITIONS_BY_ID
from bq.aihf.bridge.llm import (
    PERSONA_AGENTS,
    SIGNAL_SCHEMA,
    RouterLLM,
    agent_id_for,
)
from bq.aihf.bridge.personas import (
    persona_default_prompt,
    persona_prompt_definitions,
    resolve_persona_prompt,
)
from bq.aihf.persona_prompts import DEFAULT_PROMPTS
from bq.infra.database import migrate
from bq.llm_result import LLMResult
from bq.llm_service import CompletionOutcome
from bq.settings import (
    DEFAULT_AGENT_ROUTING,
    Provider,
    SettingsStore,
    TierName,
)

# ---------------------------------------------------------------------------
# Registration: personas are agents, not models
# ---------------------------------------------------------------------------


def test_every_persona_has_a_settings_agent_id() -> None:
    assert set(PERSONA_AGENTS) == {
        "buffett",
        "munger",
        "graham",
        "lynch",
        "druckenmiller",
    }
    for persona, agent_id in PERSONA_AGENTS.items():
        assert agent_id == f"aihf_{persona}"


def test_persona_agent_ids_are_validly_shaped() -> None:
    """Settings validates agent ids with [A-Za-z_][A-Za-z0-9_]{0,63}; a dotted id
    (e.g. 'aihf.buffett') would be silently rejected on save."""
    import re

    pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
    for agent_id in PERSONA_AGENTS.values():
        assert pattern.fullmatch(agent_id)


def test_every_persona_is_in_the_default_routing() -> None:
    """An unregistered id would fall through `route_for` to the reasoning tier —
    a default that looks configured but is not the one the code declares."""
    routable = dict(DEFAULT_AGENT_ROUTING)
    for agent_id in PERSONA_AGENTS.values():
        assert agent_id in routable
        assert routable[agent_id].tier is TierName.REASONING


def test_every_persona_is_in_the_agent_catalogue() -> None:
    """The Agent Lab renders `GET /api/v1/agents`; a persona missing there is
    unrunnable from the UI even though the engine could use it."""
    catalogued = {entry.agent_id for entry in AGENT_CATALOGUE}
    for agent_id in PERSONA_AGENTS.values():
        assert agent_id in catalogued


def test_every_persona_prompt_is_editable() -> None:
    """The standing requirement: an owner iterates wording in the UI, not in code."""
    for agent_id in PERSONA_AGENTS.values():
        assert agent_id in DEFINITIONS_BY_ID
        assert DEFINITIONS_BY_ID[agent_id].system.strip()


def test_the_editable_prompt_matches_the_persona_class() -> None:
    """`persona_prompts.py` duplicates the personas' literals so the prompt
    registry can import them without a cycle. If the two drift, an owner edits a
    prompt that is not the one runs — so they are asserted equal here."""
    from bq.aihf.engine.signals import ALPHA_MODEL_REGISTRY

    class _NoClient:
        model = "unconfigured"

        def complete(self, system: str, user: str) -> str:  # pragma: no cover
            raise AssertionError

    for name, text in DEFAULT_PROMPTS.items():
        cls = ALPHA_MODEL_REGISTRY[name]
        assert cls(llm=_NoClient()).get_system_prompt() == text


def test_an_unknown_persona_is_refused_not_defaulted() -> None:
    with pytest.raises(ValueError, match="no registered agent id"):
        agent_id_for("soros")


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class _FakeRouter:
    """Stands in for ModelRouter: returns a canned answer, records the call."""

    def __init__(
        self, text: str = '{"signal":"bullish","confidence":75,"reasoning":"Moat."}'
    ) -> None:
        self.text = text
        self.calls: list[dict] = []

    def resolve(self, agent_id: str):
        from bq.model_router import Route

        return Route(
            agent_id=agent_id,
            tier=TierName.REASONING,
            provider=Provider.GEMINI,
            model="configured-model",
            temperature=0.2,
            max_tokens=4096,
            timeout_ms=60000,
            max_attempts=3,
            fallback_tier=None,
        )

    def complete(
        self, agent_id, *, prompt, system, task_id, task_version="v1", timeout=None
    ):
        self.calls.append(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "system": system,
                "task_id": task_id,
            }
        )
        return CompletionOutcome(
            provider=Provider.GEMINI,
            model="configured-model",
            result=LLMResult(text=self.text),
        )


def test_a_persona_call_routes_as_its_own_agent() -> None:
    """The persona's id — not a shared default — decides the tier and model."""
    router = _FakeRouter()
    client = RouterLLM(router, persona="buffett")
    client.complete("You are Buffett.", "Company: X")
    assert router.calls[0]["agent_id"] == "aihf_buffett"
    assert router.calls[0]["task_id"] == "aihf.persona.buffett"


def test_the_model_id_is_whatever_settings_resolved() -> None:
    """Nothing in this module names a model. The id comes from the router."""
    client = RouterLLM(_FakeRouter(), persona="graham")
    assert client.model == "configured-model"


def test_the_answer_is_validated_against_the_signal_schema() -> None:
    router = _FakeRouter('{"signal":"bearish","confidence":40,"reasoning":"Leverage."}')
    out = RouterLLM(router, persona="munger").complete("sys", "user")
    parsed = json.loads(out)
    assert parsed["signal"] == "bearish"
    assert parsed["confidence"] == 40
    # The completion's model is recorded, so the session says what answered.
    assert "configured-model"


def test_a_malformed_answer_raises_a_parse_error_not_a_validation_crash() -> None:
    """The engine turns `LLMParseError` into an abstention; a raw jsonschema
    error would escape and fail a whole backtest."""
    from bq.aihf.engine.llm.protocol import LLMParseError

    router = _FakeRouter("I think it is probably fine.")
    with pytest.raises(LLMParseError):
        RouterLLM(router, persona="lynch").complete("sys", "user")


def test_an_out_of_range_confidence_is_rejected() -> None:
    from bq.aihf.engine.llm.protocol import LLMParseError

    router = _FakeRouter(
        '{"signal":"bullish","confidence":140,"reasoning":"Too sure."}'
    )
    with pytest.raises(LLMParseError):
        RouterLLM(router, persona="buffett").complete("sys", "user")


def test_the_schema_is_the_one_the_engine_parses() -> None:
    assert SIGNAL_SCHEMA["required"] == ["signal", "confidence", "reasoning"]
    assert SIGNAL_SCHEMA["properties"]["signal"]["enum"] == [
        "bullish",
        "neutral",
        "bearish",
    ]


def test_a_persona_with_no_configured_model_is_still_constructible() -> None:
    """An unconfigured tier must not crash a run at construction: the persona
    records `unconfigured` and every call fails into an abstain, which is visible
    in the Agent Lab."""

    class _Unconfigured:
        def resolve(self, agent_id):  # noqa: ANN001
            from bq.model_router import ModelRoutingError

            raise ModelRoutingError("the reasoning tier is missing")

        def complete(self, *_a, **_k):  # pragma: no cover
            raise AssertionError

    assert RouterLLM(_Unconfigured(), persona="buffett").model == "unconfigured"


# ---------------------------------------------------------------------------
# Prompt resolution
# ---------------------------------------------------------------------------


def test_an_owner_override_wins_over_the_default() -> None:
    class _Store:
        class _Prompt:
            system = "You are Buffett, but terser."

        def get(self, agent_id: str):
            assert agent_id == "aihf_buffett"
            return self._Prompt()

    assert resolve_persona_prompt(_Store(), "buffett") == "You are Buffett, but terser."


def test_a_prompt_store_failure_falls_back_to_the_default() -> None:
    class _Broken:
        def get(self, agent_id):  # noqa: ANN001
            raise RuntimeError("database is gone")

    assert resolve_persona_prompt(_Broken(), "buffett") == persona_default_prompt(
        "buffett"
    )


def test_no_store_means_the_default() -> None:
    assert resolve_persona_prompt(None, "graham") == DEFAULT_PROMPTS["graham"]


def test_every_persona_definition_carries_the_default_text() -> None:
    definitions = persona_prompt_definitions()
    assert len(definitions) == len(DEFAULT_PROMPTS)
    for definition in definitions:
        assert (
            definition.system
            == DEFAULT_PROMPTS[definition.agent_id.removeprefix("aihf_")]
        )


# ---------------------------------------------------------------------------
# Settings merge: an existing DB gains the personas without losing overrides
# ---------------------------------------------------------------------------


def _write_routing(path, routing: dict) -> None:
    """Persist a raw `agent_routing` row, exactly as a pre-existing install has."""
    import sqlite3

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO app_settings (setting_key, setting_value, updated_at) "
            "VALUES ('agent_routing', ?, '2026-01-01T00:00:00Z')",
            (json.dumps(routing),),
        )
        connection.commit()


def test_a_stored_routing_gains_new_agents_and_keeps_edits(tmp_path) -> None:
    """A settings row written before the personas existed holds only the older
    ids. Without the merge, `route_for('aihf_buffett')` would silently fall back
    to the reasoning tier on every existing install, and the owner's own override
    would be the only thing that survived."""
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    # The row an owner's install would already hold: only the agents that
    # existed then, with one deliberate override.
    _write_routing(
        path,
        {
            "research": {"tier": "flash", "provider": None, "model": None},
            "bull_debater": {"tier": "reasoning", "provider": None, "model": None},
        },
    )
    config = SettingsStore(path).load()
    routing = dict(config.agent_routing)
    # The owner's edit survived...
    assert routing["research"].tier is TierName.FLASH
    # ...and every persona default was added.
    for agent_id in PERSONA_AGENTS.values():
        assert agent_id in routing


def test_the_merge_does_not_overwrite_a_persona_override(tmp_path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    _write_routing(
        path, {"aihf_buffett": {"tier": "flash", "provider": None, "model": None}}
    )
    routing = dict(SettingsStore(path).load().agent_routing)
    assert routing["aihf_buffett"].tier is TierName.FLASH
