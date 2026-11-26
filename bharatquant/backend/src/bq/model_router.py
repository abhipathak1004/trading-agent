"""Config-driven model routing and the decision availability ladder.

Text agents resolve a tier from ``app_settings``. Decision agents use a typed
battery and walk a configurable ladder: native Jev, then the configured text
tiers, then the deterministic heuristic, then a rules-only floor. The router is
the only place that knows this order (ADR-016, ADR-017, ADR-018).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx

from bq.agent_prompts import DECISION_SYSTEM, PromptSource, resolve_prompt
from bq.decision.battery import decision_prompt, decision_schema, questions_for
from bq.decision.client import JevClient, JevError, parse_decisions
from bq.decision.fallback import HeuristicJudge
from bq.decision.policy import DecisionPolicy
from bq.decision.state import DecisionState
from bq.decision.types import (
    DecisionOutcome,
    JudgmentSet,
    Question,
)
from bq.llm_client import LLMNotConfigured, ModelClient, client_for
from bq.llm_service import (
    ALLOWED_DATA,
    HOSTED_CLASSES,
    CompletionOutcome,
)
from bq.model_routing import (
    DataClassification,
    ModelCandidate,
    ModelTier,
    RouteUnavailable,
    RoutingError,
    TaskClass,
    TaskPolicy,
    select_model,
)
from bq.settings import (
    AppConfig,
    DecisionFallback,
    DecisionRoute,
    Provider,
    SettingsStore,
    TierName,
)
from bq.structured_output import complete_structured

TASK_AGENTS = {
    "analysis.bull": "bull_debater",
    "analysis.bear": "bear_debater",
    "analysis.judge": "research_judge",
    "analysis.risk": "risk_analyst",
}
DEFAULT_AGENT_ID = "research_judge"
DECISION_TASK = "decision.battery"


class DecisionUnavailable(ValueError):
    """No configured source could answer the decision battery."""


class ModelRoutingError(ValueError):
    """A route request is inconsistent with the configured tiers."""


@dataclass(frozen=True, slots=True)
class Route:
    agent_id: str
    tier: TierName
    provider: Provider
    model: str
    temperature: float
    max_tokens: int
    timeout_ms: int
    max_attempts: int
    fallback_tier: TierName | None


@dataclass(frozen=True, slots=True)
class RouteUse:
    """One row in the run ledger: exactly what answered and how."""

    agent_id: str
    task_id: str
    tier: str
    provider: str
    model: str
    decision_source: str
    fallback_used: bool
    fallback_from: str | None
    latency_ms: float | None
    request_id: str | None
    model_mismatch: bool
    state_hash: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "decision_source": self.decision_source,
            "fallback_used": self.fallback_used,
            "fallback_from": self.fallback_from,
            "latency_ms": self.latency_ms,
            "request_id": self.request_id,
            "model_mismatch": self.model_mismatch,
            "state_hash": self.state_hash,
        }


@dataclass(frozen=True, slots=True)
class DecisionAvailability:
    preview: str
    jev_configured: bool
    fallbacks: tuple[str, ...]


class ModelRouter:
    def __init__(
        self,
        store: SettingsStore,
        *,
        clients: Mapping[Provider, ModelClient] | None = None,
        jev: JevClient | None = None,
        transport: httpx.BaseTransport | None = None,
        prompts: PromptSource | None = None,
    ) -> None:
        self.store = store
        self._clients = dict(clients or {})
        self._jev = jev
        self._transport = transport
        self._prompts = prompts
        self._routes: list[RouteUse] = []

    @property
    def routes_used(self) -> tuple[RouteUse, ...]:
        return tuple(self._routes)

    def availability(self) -> DecisionAvailability:
        config = self.store.load()
        jev = self._jev_available(config)
        fallbacks: list[str] = []
        for fallback in config.pipeline.decision_fallback_order:
            if fallback is DecisionFallback.HEURISTIC:
                fallbacks.append("heuristic")
                continue
            if config.tier_configured(TierName(fallback.value)):
                fallbacks.append(fallback.value)
        return DecisionAvailability(
            preview=config.decision_preview,
            jev_configured=jev,
            fallbacks=tuple(fallbacks),
        )

    # -- text tiers -------------------------------------------------------

    def resolve(self, agent_id: str, *, task_id: str = "") -> Route:
        config = self.store.load()
        route = config.route_for(agent_id)
        if route.tier is TierName.DECISION:
            raise ModelRoutingError(
                "Decision agents must call decide(), not complete()."
            )
        return self._route_for_tier(
            route.tier,
            agent_id,
            provider_override=route.provider,
            model_override=route.model,
        )

    def complete(
        self,
        agent_id: str,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str = "v1",
        timeout: float | None = None,
    ) -> CompletionOutcome:
        route = self.resolve(agent_id, task_id=task_id)
        try:
            outcome = self._complete_route(
                route,
                prompt=prompt,
                system=system,
                task_id=task_id,
                task_version=task_version,
                timeout=timeout,
            )
        except Exception as primary:
            if route.fallback_tier is None:
                raise
            fallback = self._route_for_tier(route.fallback_tier, agent_id)
            try:
                outcome = self._complete_route(
                    fallback,
                    prompt=prompt,
                    system=system,
                    task_id=task_id,
                    task_version=task_version,
                    timeout=timeout,
                )
            except Exception:
                raise primary
            self._record(
                RouteUse(
                    agent_id=agent_id,
                    task_id=task_id,
                    tier=fallback.tier.value,
                    provider=fallback.provider.value,
                    model=fallback.model,
                    decision_source="llm_fallback",
                    fallback_used=True,
                    fallback_from=route.tier.value,
                    latency_ms=None,
                    request_id=None,
                    model_mismatch=False,
                    state_hash=None,
                )
            )
            return outcome
        self._record(
            RouteUse(
                agent_id=agent_id,
                task_id=task_id,
                tier=route.tier.value,
                provider=route.provider.value,
                model=route.model,
                decision_source="llm_direct",
                fallback_used=False,
                fallback_from=None,
                latency_ms=None,
                request_id=None,
                model_mismatch=False,
                state_hash=None,
            )
        )
        return outcome

    # -- decision ladder --------------------------------------------------

    def decide(
        self,
        agent_id: str,
        *,
        state: DecisionState,
        battery: str,
        task_id: str = "",
        claims: Sequence[str] = (),
    ) -> DecisionOutcome:
        config = self.store.load()
        questions = questions_for(battery, claims=claims)
        label = task_id or f"{DECISION_TASK}.{battery}"
        failure = "unavailable"

        if self._jev_available(config):
            try:
                outcome = self._decide_jev(config, state, battery, questions)
                self._record_decision(agent_id, label, outcome)
                return outcome
            except (JevError, LLMNotConfigured, ValueError) as exc:
                failure = type(exc).__name__

        for fallback in config.pipeline.decision_fallback_order:
            if fallback is DecisionFallback.HEURISTIC:
                try:
                    judgments = HeuristicJudge().judge(
                        state=state, battery=battery, claims=claims
                    )
                except ValueError:
                    continue
                outcome = DecisionOutcome(
                    judgments=judgments,
                    provider="code",
                    model=judgments.model,
                    tier="heuristic",
                    fallback_used=True,
                    fallback_from=failure,
                    state_hash=state.digest(),
                )
                self._record_decision(agent_id, label, outcome)
                return outcome
            try:
                outcome = self._decide_with_llm(
                    config,
                    TierName(fallback.value),
                    agent_id,
                    state,
                    battery,
                    questions,
                    failure,
                )
            except Exception as exc:
                failure = type(exc).__name__
                continue
            self._record_decision(agent_id, label, outcome)
            return outcome
        raise DecisionUnavailable(
            f"No decision source could answer the {battery} battery ({failure})."
        )

    # -- internals --------------------------------------------------------

    def _jev_available(self, config: AppConfig) -> bool:
        if config.decision_route not in (
            DecisionRoute.AUTO,
            DecisionRoute.TYPESAFE_JEV,
        ):
            return False
        tier = config.tier(TierName.DECISION)
        if tier is None or not tier.model or tier.provider is not Provider.TYPESAFE_JEV:
            return False
        return self.store.api_key(Provider.TYPESAFE_JEV) is not None

    def _decide_jev(
        self,
        config: AppConfig,
        state: DecisionState,
        battery: str,
        questions: Sequence[Question],
    ) -> DecisionOutcome:
        tier = config.tier(TierName.DECISION)
        if tier is None or tier.provider is not Provider.TYPESAFE_JEV:
            raise LLMNotConfigured("The decision tier is not configured for Jev.")
        api_key = self._key(Provider.TYPESAFE_JEV)
        client = self._jev or JevClient(transport=self._transport)
        judgments = client.decide(
            model=tier.model,
            api_key=api_key,
            state=state.to_state_dict(),
            questions=questions,
            timeout=tier.timeout_ms / 1000.0,
            max_attempts=tier.max_attempts,
            battery=battery,
        )
        return DecisionOutcome(
            judgments=judgments,
            provider=Provider.TYPESAFE_JEV.value,
            model=judgments.model,
            tier=TierName.DECISION.value,
            fallback_used=False,
            fallback_from=None,
            state_hash=state.digest(),
        )

    def _decide_with_llm(
        self,
        config: AppConfig,
        tier_name: TierName,
        agent_id: str,
        state: DecisionState,
        battery: str,
        questions: Sequence[Question],
        fallback_from: str,
    ) -> DecisionOutcome:
        tier = config.tier(tier_name)
        if tier is None or tier.provider is None or not tier.model:
            raise LLMNotConfigured(f"The {tier_name.value} tier is not configured.")
        self._key(tier.provider)
        prompt = decision_prompt(state.to_state_dict(), questions)
        schema = decision_schema(questions)
        completer = _TierCompleter(self, tier_name, agent_id)
        system, _template = resolve_prompt(
            self._prompts, "decision_screener", DECISION_SYSTEM, ""
        )
        parsed, completion = complete_structured(
            completer,
            prompt=prompt,
            system=system,
            schema=schema,
            task_id=f"decision.{battery}",
            timeout=tier.timeout_ms / 1000.0,
        )
        if not isinstance(parsed, Mapping):
            raise ModelRoutingError("The fallback answer is not a JSON object.")
        answers = parse_decisions(parsed, questions)
        judgments = JudgmentSet(
            battery=battery,
            answers=answers,
            model=completion.model,
            source="llm_fallback",
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
        )
        return DecisionOutcome(
            judgments=judgments,
            provider=completion.provider.value,
            model=completion.model,
            tier=tier_name.value,
            fallback_used=True,
            fallback_from=fallback_from,
            state_hash=state.digest(),
        )

    def _route_for_tier(
        self,
        tier_name: TierName,
        agent_id: str,
        *,
        provider_override: Provider | None = None,
        model_override: str | None = None,
    ) -> Route:
        config = self.store.load()
        tier = config.tier(tier_name)
        if tier is None:
            raise ModelRoutingError(f"The {tier_name.value} tier is missing.")
        provider = provider_override or tier.provider
        model = model_override or tier.model
        if provider is None or not model:
            raise LLMNotConfigured(f"The {tier_name.value} tier has no model.")
        if provider is Provider.TYPESAFE_JEV:
            raise LLMNotConfigured("TypeSafe Jev is a decision provider only.")
        self._key(provider)
        return Route(
            agent_id=agent_id,
            tier=tier_name,
            provider=provider,
            model=model,
            temperature=tier.temperature,
            max_tokens=tier.max_tokens,
            timeout_ms=tier.timeout_ms,
            max_attempts=tier.max_attempts,
            fallback_tier=tier.fallback_tier,
        )

    def _key(self, provider: Provider) -> str:
        key = self.store.api_key(provider)
        if key is None:
            raise LLMNotConfigured(
                f"No API key is configured for provider {provider.value}."
            )
        return key

    def _client(self, provider: Provider) -> ModelClient:
        client = self._clients.get(provider)
        if client is not None:
            return client
        return client_for(provider, transport=self._transport)

    def _complete_route(
        self,
        route: Route,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str,
        timeout: float | None,
    ) -> CompletionOutcome:
        api_key = self._key(route.provider)
        candidate = ModelCandidate(
            model_id=route.model,
            tier=ModelTier.PAID,
            available=True,
            capability_verified=True,
            terms_reviewed=True,
            supported_classes=HOSTED_CLASSES,
            approved_data=ALLOWED_DATA,
            model_sha256=None,
            evaluations=frozenset(),
        )
        policy = TaskPolicy(
            task_id=task_id or f"agent.{route.agent_id}",
            version=task_version,
            task_class=TaskClass.REASONING,
            allowed_data=ALLOWED_DATA,
            allowed_tiers=frozenset({ModelTier.PAID}),
        )
        try:
            select_model(
                policy,
                data_classes=frozenset({DataClassification.PUBLIC}),
                candidates=(candidate,),
            )
        except RouteUnavailable as exc:
            raise LLMNotConfigured(str(exc)) from exc
        except RoutingError as exc:
            raise LLMNotConfigured(str(exc)) from exc
        result = self._client(route.provider).complete(
            model=route.model,
            api_key=api_key,
            system=system,
            prompt=prompt,
            timeout=(route.timeout_ms / 1000.0 if timeout is None else float(timeout)),
        )
        return CompletionOutcome(
            provider=route.provider, model=route.model, result=result
        )

    def _record(self, use: RouteUse) -> None:
        self._routes.append(use)

    def _record_decision(
        self, agent_id: str, task_id: str, outcome: DecisionOutcome
    ) -> None:
        self._record(
            RouteUse(
                agent_id=agent_id,
                task_id=task_id,
                tier=outcome.tier,
                provider=outcome.provider,
                model=outcome.model,
                decision_source=outcome.source,
                fallback_used=outcome.fallback_used,
                fallback_from=outcome.fallback_from,
                latency_ms=outcome.judgments.latency_ms,
                request_id=outcome.judgments.request_id,
                model_mismatch=outcome.judgments.model_mismatch,
                state_hash=outcome.state_hash,
            )
        )


@dataclass(frozen=True, slots=True)
class _TierCompleter:
    router: ModelRouter
    tier: TierName
    agent_id: str

    def __call__(
        self,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str,
        timeout: float | None,
    ) -> CompletionOutcome:
        route = self.router._route_for_tier(self.tier, self.agent_id)
        return self.router._complete_route(
            route,
            prompt=prompt,
            system=system,
            task_id=task_id,
            task_version=task_version,
            timeout=timeout,
        )


class RoutedModelCaller:
    """Drop-in caller for ``Debate`` and ``RiskAnalyst``."""

    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        task_id: str = "assistant.text",
        task_version: str = "v1",
    ) -> CompletionOutcome:
        agent_id = TASK_AGENTS.get(task_id, DEFAULT_AGENT_ID)
        return self.router.complete(
            agent_id,
            prompt=prompt,
            system=system,
            task_id=task_id,
            task_version=task_version,
        )

    @property
    def routes_used(self) -> tuple[RouteUse, ...]:
        return self.router.routes_used


def decision_policy_for(config: AppConfig) -> DecisionPolicy:
    policy = config.decision_policy
    return DecisionPolicy(
        screener_min_score=config.pipeline.screener_min_score,
        final_decision_min_prob=config.pipeline.final_decision_min_prob,
        min_choice_confidence=policy.min_choice_confidence,
        min_noul_probability=policy.min_noul_probability,
        abstain_score_band=policy.abstain_score_band,
        llm_fallback_size_scale=policy.llm_fallback_size_scale,
        heuristic_size_scale=policy.heuristic_size_scale,
    )


__all__ = [
    "DEFAULT_AGENT_ID",
    "DecisionAvailability",
    "DecisionUnavailable",
    "ModelRouter",
    "ModelRoutingError",
    "Route",
    "RouteUse",
    "RoutedModelCaller",
    "TASK_AGENTS",
    "decision_policy_for",
]
