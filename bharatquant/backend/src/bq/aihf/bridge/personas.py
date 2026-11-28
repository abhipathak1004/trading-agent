"""Staffing a fund's personas with the app's LLM client and editable prompts.

Two seams meet here.

**The client.** The engine's personas take an ``LLMClient``; each must route as
its own Settings agent (``aihf_buffett``, …) so a tier or model override is a
settings change. :func:`staff` builds the persona with the right ``RouterLLM``.

**The prompt.** Upstream a persona's system prompt is a string literal in
``get_system_prompt()``. Here it is *configuration*: the literal is only the
default, and an owner's edit in the Agent Lab overrides it. Both halves exist
because the standing requirement is that every agent's wording can be iterated
from the UI without a code change — and a persona's whole edge is its wording.

The default text is read from the vendored class itself rather than copied into
``agent_prompts.py``: one source of truth, so an upstream reminder to bump a
persona's wording flows through automatically instead of drifting.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bq.aihf.bridge.llm import PERSONA_AGENTS, RouterLLM, agent_id_for
from bq.aihf.engine.llm.protocol import LLMClient
from bq.aihf.engine.signals import ALPHA_MODEL_REGISTRY
from bq.aihf.engine.signals.llm_agent import LLMAgent
from bq.model_router import ModelRouter

if TYPE_CHECKING:
    from bq.aihf.engine.fund.spec import FundSpec
    from bq.aihf.engine.llm import PromptCache
    from bq.aihf.engine.signals.base import AlphaModel


@dataclass(frozen=True, slots=True)
class PersonaPrompt:
    """A persona's editable prompt, shaped for `bq.agent_prompts`.

    Deliberately a local dataclass rather than importing `PromptDefinition`:
    `bq.agent_prompts` registers these personas, so importing it here would close
    a cycle at module load. The field names match, so `agent_prompts` can spread
    these straight into its own definitions.
    """

    agent_id: str
    label: str
    description: str
    system: str
    template: str = ""
    required_fields: tuple[str, ...] = ()


# The personas this app knows how to staff. A mandate naming any other model id
# still works if it is a registered quant model (pead); a persona not in this
# map has no Settings agent to route as, which `agent_id_for` refuses.
PERSONAS: tuple[str, ...] = tuple(PERSONA_AGENTS)


def persona_default_prompt(name: str) -> str:
    """The persona's default system prompt.

    Read from `bq.aihf.persona_prompts` (the vendored literals, verbatim) rather
    than by instantiating the class: constructing a persona needs an LLM client,
    and reading its literal must not. One source of truth for the text; a test
    asserts it still equals what the persona class returns.
    """
    from bq.aihf.persona_prompts import DEFAULT_PROMPTS

    try:
        return DEFAULT_PROMPTS[name]
    except KeyError as exc:
        raise ValueError(f"{name!r} is not a known persona") from exc


def persona_prompt_definitions() -> tuple[PersonaPrompt, ...]:
    """One prompt per persona, defaulting to its upstream wording."""
    from bq.aihf.persona_prompts import DEFAULT_PROMPTS

    return tuple(
        PersonaPrompt(
            agent_id=agent_id_for(name),
            label=f"{name.title()} persona",
            description=(
                f"System prompt for the AI Hedge Fund's {name.title()} investor "
                "persona. The facts it reads are assembled point-in-time by the "
                "engine and are not editable."
            ),
            system=text,
        )
        for name, text in DEFAULT_PROMPTS.items()
    )


def resolve_persona_prompt(prompts: object | None, name: str) -> str:
    """The prompt to use: the owner's edit when one exists, else upstream's.

    *prompts* is any object with ``get(agent_id)`` returning something with a
    ``.system`` attribute — the shape `bq.agent_prompts.PromptStore` already has.
    Duck-typed rather than imported so this module never imports the registry
    that imports it.
    """
    default = persona_default_prompt(name)
    getter = getattr(prompts, "get", None)
    if not callable(getter):
        return default
    try:
        prompt = getter(agent_id_for(name))
    except Exception:  # noqa: BLE001 - a prompt store failure must not break a run
        return default
    system = getattr(prompt, "system", None)
    return system if isinstance(system, str) and system.strip() else default


def staff(
    spec: "FundSpec",
    *,
    router: ModelRouter,
    prompts: object | None = None,
    blind: bool = False,
    cache: "PromptCache | None" = None,
) -> dict[str, list["AlphaModel"]]:
    """Build every strategy's staff, with per-persona clients and prompts.

    Returned keyed by strategy name, which is exactly the ``models=`` argument
    ``Fund`` accepts. A quant-only pod (``pead``) is built the plain way and is
    unaffected. Every persona is constructed with:
      * its own ``RouterLLM``, so it routes as its own Settings agent;
      * ``system_prompt`` from the prompt store (the upstream literal is the
        default), so its wording is editable without a code change.
    """
    staffed: dict[str, list[AlphaModel]] = {}
    for strategy in spec.strategies:
        members: list[AlphaModel] = []
        for model in strategy.models:
            cls = ALPHA_MODEL_REGISTRY.get(model.name)
            if cls is None:
                raise ValueError(f"unknown model {model.name!r}")
            params = dict(model.params)
            if issubclass(cls, LLMAgent):
                if blind:
                    params["blind"] = True
                members.append(
                    cls(
                        llm=RouterLLM(router, persona=model.name),
                        cache=cache,
                        system_prompt=resolve_persona_prompt(prompts, model.name),
                        **params,
                    )
                )
            else:
                members.append(cls(**params))
        staffed[strategy.name] = members
    return staffed


def persona_client_factory(router: ModelRouter) -> Callable[[str], LLMClient]:
    """A ``persona -> LLMClient`` factory, for callers that build their own staff."""
    return lambda persona: RouterLLM(router, persona=persona)


__all__ = [
    "PERSONAS",
    "persona_client_factory",
    "persona_default_prompt",
    "persona_prompt_definitions",
    "resolve_persona_prompt",
    "staff",
]
