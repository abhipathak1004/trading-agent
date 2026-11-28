"""The single source of truth for every agent in the harness.

This module joins the two existing registries — editable prompts
(``bq.agent_prompts``) and per-agent model routes (``bq.settings``) — with the
metadata the UI needs to render one tab per agent. It does not build or run
agents; it only describes them. The API enriches loop-agent entries with the
live tool list at request time.
"""

from __future__ import annotations

from dataclasses import dataclass

from bq.settings import DEFAULT_AGENT_ROUTING, AgentRoute, AppConfig, TierName

KIND_LOOP = "loop"
KIND_DAG_NODE = "dag_node"
KIND_DECISION = "decision"
KIND_COORDINATOR = "coordinator"
KIND_DETERMINISTIC = "deterministic"

RESEARCH_TOOLS: tuple[str, ...] = (
    "news_search",
    "news_memory",
    "news_digest",
    "news_ingest",
    "stock_quote",
    "stock_download",
    "web_fetch",
    "response",
    "unknown",
)


@dataclass(frozen=True, slots=True)
class AgentCatalogueEntry:
    agent_id: str
    kind: str
    label: str
    description: str
    tier: TierName
    prompt_id: str | None = None
    tools: tuple[str, ...] = ()
    read_only: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "tier": self.tier.value,
            "prompt_id": self.prompt_id,
            "tools": list(self.tools),
            "read_only": self.read_only,
        }


_DEFAULT_ROUTES = dict(DEFAULT_AGENT_ROUTING)


def _tier(agent_id: str) -> TierName:
    route = _DEFAULT_ROUTES.get(agent_id)
    return TierName.REASONING if route is None else route.tier


AGENT_CATALOGUE: tuple[AgentCatalogueEntry, ...] = (
    AgentCatalogueEntry(
        agent_id="research",
        kind=KIND_LOOP,
        label="Research agent",
        description=(
            "Interactive research loop over news, market data and web tools. "
            "The model may call tools; the trace is observable."
        ),
        tier=_tier("research"),
        prompt_id="research",
        tools=RESEARCH_TOOLS,
        read_only=False,
    ),
    AgentCatalogueEntry(
        agent_id="news_research",
        kind=KIND_DETERMINISTIC,
        label="News research agent",
        description=(
            "Fetches, deduplicates and stores news. Deterministic; makes no "
            "model calls."
        ),
        tier=_tier("news_research"),
        prompt_id="news_research",
        read_only=False,
    ),
    AgentCatalogueEntry(
        agent_id="bull_debater",
        kind=KIND_DAG_NODE,
        label="Bull advocate",
        description="Argues the strongest honest long case from the supplied facts.",
        tier=_tier("bull_debater"),
        prompt_id="bull_debater",
    ),
    AgentCatalogueEntry(
        agent_id="bear_debater",
        kind=KIND_DAG_NODE,
        label="Bear advocate",
        description="Argues the strongest honest case against the long position.",
        tier=_tier("bear_debater"),
        prompt_id="bear_debater",
    ),
    AgentCatalogueEntry(
        agent_id="research_judge",
        kind=KIND_DAG_NODE,
        label="Research judge",
        description="Weighs bull and bear against the facts and the confidence ceiling.",
        tier=_tier("research_judge"),
        prompt_id="research_judge",
    ),
    AgentCatalogueEntry(
        agent_id="risk_analyst",
        kind=KIND_DAG_NODE,
        label="Risk analyst",
        description="Describes invalidation and early warnings; advisory only.",
        tier=_tier("risk_analyst"),
        prompt_id="risk_analyst",
    ),
    AgentCatalogueEntry(
        agent_id="super_agent_cio",
        kind=KIND_COORDINATOR,
        label="Super Agent (CIO)",
        description=(
            "Ranks the specialist results against the owner's strategy and "
            "writes the portfolio view. Cannot create a BUY the deterministic "
            "plan rejects."
        ),
        tier=_tier("super_agent_cio"),
        prompt_id="super_agent_cio",
    ),
    AgentCatalogueEntry(
        agent_id="watchlist_screener",
        kind=KIND_DECISION,
        label="Watchlist screener",
        description="Atomic decision battery: is this setup worth a deeper pass?",
        tier=_tier("watchlist_screener"),
    ),
    AgentCatalogueEntry(
        agent_id="news_triage",
        kind=KIND_DECISION,
        label="News triage",
        description="Atomic decision battery: materiality, category and horizon.",
        tier=_tier("news_triage"),
    ),
    AgentCatalogueEntry(
        agent_id="evidence_verifier",
        kind=KIND_DECISION,
        label="Evidence verifier",
        description="Checks whether supplied claims are supported by the facts.",
        tier=_tier("evidence_verifier"),
    ),
    AgentCatalogueEntry(
        agent_id="final_decision_gate",
        kind=KIND_DECISION,
        label="Final decision gate",
        description="Atomic decision battery: final action and execute-trade judgment.",
        tier=_tier("final_decision_gate"),
    ),
)


@dataclass(frozen=True, slots=True)
class AnnotatedAgent:
    agent_id: str
    kind: str
    label: str
    description: str
    tier: str
    prompt_id: str | None
    tools: tuple[str, ...]
    read_only: bool
    route_tier: str
    provider: str | None
    model: str | None
    configured: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "tier": self.tier,
            "prompt_id": self.prompt_id,
            "tools": list(self.tools),
            "read_only": self.read_only,
            "route": {
                "tier": self.route_tier,
                "provider": self.provider,
                "model": self.model,
                "configured": self.configured,
            },
        }


def catalogue_for(config: AppConfig) -> tuple[AnnotatedAgent, ...]:
    """Annotate the static catalogue with the currently resolved routes."""
    annotated: list[AnnotatedAgent] = []
    for entry in AGENT_CATALOGUE:
        route: AgentRoute = config.route_for(entry.agent_id)
        tier = config.tier(route.tier)
        provider = None if tier is None else tier.provider
        model = None if tier is None else tier.model
        configured = config.tier_configured(route.tier)
        annotated.append(
            AnnotatedAgent(
                agent_id=entry.agent_id,
                kind=entry.kind,
                label=entry.label,
                description=entry.description,
                tier=entry.tier.value,
                prompt_id=entry.prompt_id,
                tools=entry.tools,
                read_only=entry.read_only,
                route_tier=route.tier.value,
                provider=None if provider is None else provider.value,
                model=model,
                configured=configured,
            )
        )
    return tuple(annotated)


__all__ = [
    "AGENT_CATALOGUE",
    "AgentCatalogueEntry",
    "AnnotatedAgent",
    "KIND_COORDINATOR",
    "KIND_DAG_NODE",
    "KIND_DECISION",
    "KIND_DETERMINISTIC",
    "KIND_LOOP",
    "RESEARCH_TOOLS",
    "catalogue_for",
]
