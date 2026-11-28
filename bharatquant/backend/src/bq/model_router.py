"""Config-driven model routing and the decision availability ladder.

Text agents resolve a tier from ``app_settings``. Decision agents use a typed
battery and walk a configurable ladder: native Jev, then the configured text
tiers, then the deterministic heuristic, then a rules-only floor. The router is
the only place that knows this order (ADR-016, ADR-017, ADR-018).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx

from bq.agent_prompts import DECISION_SYSTEM, PromptSource, resolve_prompt
from bq.core import canonical_digest
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
from bq.infra.model_attempts import AttemptOutcome, AttemptStatus, UsageKind
from bq.infra.task_registry import TaskDefinitionNotFound, TaskRegistry
from bq.llm_client import (
    LLMNotConfigured,
    ModelClient,
    ToolCallRequest,
    ToolResult,
    client_for,
)
from bq.llm_gateway import LLMGateway, build_prompt_context
from bq.llm_result import LLMResult
from bq.llm_service import (
    ALLOWED_DATA,
    HOSTED_CLASSES,
    CompletionOutcome,
)
from bq.logbook import record_decision, record_llm_call
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
from bq.task_registry import TaskDefinition

TASK_AGENTS = {
    "analysis.bull": "bull_debater",
    "analysis.bear": "bear_debater",
    "analysis.judge": "research_judge",
    "analysis.risk": "risk_analyst",
    "research.agent": "research",
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
        tools: Sequence[Mapping[str, object]] | None = None,
        tool_call: ToolCallRequest | None = None,
        tool_result: ToolResult | None = None,
    ) -> CompletionOutcome:
        route = self.resolve(agent_id, task_id=task_id)
        task = self._registered_task(task_id, task_version)

        def run(selected: Route) -> CompletionOutcome:
            if task is None:
                return self._complete_route(
                    selected,
                    prompt=prompt,
                    system=system,
                    task_id=task_id,
                    task_version=task_version,
                    timeout=timeout,
                    tools=tools,
                    tool_call=tool_call,
                    tool_result=tool_result,
                )
            return self._complete_governed(
                task,
                selected,
                prompt=prompt,
                system=system,
                task_id=task_id,
                task_version=task_version,
                timeout=timeout,
            )

        try:
            outcome = run(route)
        except Exception as primary:
            if route.fallback_tier is None:
                raise
            fallback = self._route_for_tier(route.fallback_tier, agent_id)
            try:
                outcome = run(fallback)
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
        state: DecisionState | Mapping[str, object],
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
                outcome = self._decide_jev(config, state, battery, questions, agent_id)
                self._record_decision(agent_id, label, outcome)
                return outcome
            except (JevError, LLMNotConfigured, ValueError) as exc:
                failure = type(exc).__name__

        for fallback in config.pipeline.decision_fallback_order:
            if fallback is DecisionFallback.HEURISTIC:
                if not isinstance(state, DecisionState):
                    continue
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
                    state_hash=self._state_hash(state),
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
        state: DecisionState | Mapping[str, object],
        battery: str,
        questions: Sequence[Question],
        agent_id: str,
    ) -> DecisionOutcome:
        tier = config.tier(TierName.DECISION)
        if tier is None or tier.provider is not Provider.TYPESAFE_JEV:
            raise LLMNotConfigured("The decision tier is not configured for Jev.")
        api_key = self._key(Provider.TYPESAFE_JEV)
        client = self._jev or JevClient(transport=self._transport)
        payload = self._state_payload(state)
        started = time.monotonic()
        try:
            judgments = client.decide(
                model=tier.model,
                api_key=api_key,
                state=payload,
                questions=questions,
                timeout=tier.timeout_ms / 1000.0,
                max_attempts=tier.max_attempts,
                battery=battery,
            )
        except Exception as exc:
            record_llm_call(
                provider=Provider.TYPESAFE_JEV.value,
                model=tier.model,
                task_id=f"decision.{battery}",
                agent_id=agent_id,
                tier=TierName.DECISION.value,
                battery=battery,
                latency_ms=(time.monotonic() - started) * 1000.0,
                ok=False,
                error=type(exc).__name__,
                prompt=json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), default=str
                ),
                response="",
            )
            raise
        record_llm_call(
            provider=Provider.TYPESAFE_JEV.value,
            model=judgments.model,
            task_id=f"decision.{battery}",
            agent_id=agent_id,
            tier=TierName.DECISION.value,
            battery=battery,
            latency_ms=(time.monotonic() - started) * 1000.0,
            ok=True,
            input_tokens=judgments.input_tokens,
            output_tokens=judgments.output_tokens,
            prompt=json.dumps(
                payload, sort_keys=True, separators=(",", ":"), default=str
            ),
            response=json.dumps(
                judgments.to_dict(), sort_keys=True, separators=(",", ":"), default=str
            ),
        )
        return DecisionOutcome(
            judgments=judgments,
            provider=Provider.TYPESAFE_JEV.value,
            model=judgments.model,
            tier=TierName.DECISION.value,
            fallback_used=False,
            fallback_from=None,
            state_hash=self._state_hash(state),
        )

    def _decide_with_llm(
        self,
        config: AppConfig,
        tier_name: TierName,
        agent_id: str,
        state: DecisionState | Mapping[str, object],
        battery: str,
        questions: Sequence[Question],
        fallback_from: str,
    ) -> DecisionOutcome:
        tier = config.tier(tier_name)
        if tier is None or tier.provider is None or not tier.model:
            raise LLMNotConfigured(f"The {tier_name.value} tier is not configured.")
        self._key(tier.provider)
        prompt = decision_prompt(self._state_payload(state), questions)
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
            state_hash=self._state_hash(state),
        )

    @staticmethod
    def _state_payload(
        state: DecisionState | Mapping[str, object],
    ) -> dict[str, object]:
        if isinstance(state, DecisionState):
            return state.to_state_dict()
        return dict(state)

    @staticmethod
    def _state_hash(state: DecisionState | Mapping[str, object]) -> str:
        if isinstance(state, DecisionState):
            return state.digest()
        return canonical_digest(dict(state))

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
            if provider is Provider.CUSTOM:
                # A local OpenAI-compatible endpoint (e.g. Ollama) is keyless;
                # the wire client sends this placeholder in the auth header.
                return "local"
            raise LLMNotConfigured(
                f"No API key is configured for provider {provider.value}."
            )
        return key

    def _client(self, provider: Provider) -> ModelClient:
        client = self._clients.get(provider)
        if client is not None:
            return client
        config = self.store.load()
        if provider is Provider.CUSTOM:
            if config.custom_endpoint:
                return client_for(
                    provider,
                    transport=self._transport,
                    custom_endpoint=config.custom_endpoint,
                )
        if provider is Provider.BEDROCK:
            return client_for(
                provider,
                transport=self._transport,
                bedrock_region=config.bedrock_region,
            )
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
        tools: Sequence[Mapping[str, object]] | None = None,
        tool_call: ToolCallRequest | None = None,
        tool_result: ToolResult | None = None,
    ) -> CompletionOutcome:
        candidate = self._candidate_for(route)
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
        result = self._invoke(
            route,
            prompt=prompt,
            system=system,
            task_id=task_id,
            task_version=task_version,
            timeout=timeout,
            tools=tools,
            tool_call=tool_call,
            tool_result=tool_result,
        )
        return CompletionOutcome(
            provider=route.provider, model=route.model, result=result
        )

    def _complete_governed(
        self,
        task: TaskDefinition,
        route: Route,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str,
        timeout: float | None,
    ) -> CompletionOutcome:
        """Run a task-bearing call through the gateway (ADR-048)."""
        candidate = self._candidate_for(route)
        gateway = LLMGateway(
            self.store.path,
            candidates=(candidate,),
            clock=self.store.clock,
        )
        bundle = build_prompt_context(
            task,
            prompt=prompt,
            system=system,
            model_id=route.model,
            scope=route.agent_id,
            as_of=self.store.clock.now(),
        )
        selected_task, selected, manifest_hash = gateway.prepare(
            task_id=task_id,
            task_version=task_version,
            data_classes=frozenset(
                {task.system_classification, task.schema_classification}
            ),
            bundle=bundle,
            model_id=route.model,
            candidates=(candidate,),
        )
        result = self._invoke(
            route,
            prompt=prompt,
            system=system,
            task_id=task_id,
            task_version=task_version,
            timeout=timeout,
        )
        gateway.validate_output(
            task=selected_task,
            manifest_hash=manifest_hash,
            model_id=selected.model_id,
            output=result.text,
        )
        gateway.complete(
            task_id=task_id,
            task_version=task_version,
            manifest_hash=manifest_hash,
            model_id=selected.model_id,
            outcome=AttemptOutcome(
                status=AttemptStatus.SUCCEEDED,
                usage_kind=(
                    UsageKind.UNKNOWN
                    if result.input_tokens is None and result.output_tokens is None
                    else UsageKind.REPORTED
                ),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            ),
        )
        return CompletionOutcome(
            provider=route.provider, model=route.model, result=result
        )

    def _registered_task(
        self, task_id: str, task_version: str
    ) -> TaskDefinition | None:
        if not task_id or not task_version:
            return None
        try:
            return TaskRegistry(self.store.path, self.store.clock).load(
                task_id, task_version
            )
        except TaskDefinitionNotFound:
            return None

    def _candidate_for(self, route: Route) -> ModelCandidate:
        return ModelCandidate(
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

    def _invoke(
        self,
        route: Route,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str,
        timeout: float | None,
        tools: Sequence[Mapping[str, object]] | None = None,
        tool_call: ToolCallRequest | None = None,
        tool_result: ToolResult | None = None,
    ) -> LLMResult:
        api_key = self._key(route.provider)
        started = time.monotonic()
        try:
            result = self._client(route.provider).complete(
                model=route.model,
                api_key=api_key,
                system=system,
                prompt=prompt,
                timeout=(
                    route.timeout_ms / 1000.0 if timeout is None else float(timeout)
                ),
                tools=tools,
                tool_call=tool_call,
                tool_result=tool_result,
            )
        except Exception as exc:
            record_llm_call(
                provider=route.provider.value,
                model=route.model,
                task_id=task_id or f"agent.{route.agent_id}",
                task_version=task_version,
                agent_id=route.agent_id,
                tier=route.tier.value,
                latency_ms=(time.monotonic() - started) * 1000.0,
                ok=False,
                error=type(exc).__name__,
                system=system,
                prompt=prompt,
            )
            raise
        record_llm_call(
            provider=route.provider.value,
            model=route.model,
            task_id=task_id or f"agent.{route.agent_id}",
            task_version=task_version,
            agent_id=route.agent_id,
            tier=route.tier.value,
            latency_ms=(time.monotonic() - started) * 1000.0,
            ok=True,
            finish_reason=result.finish_reason,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            system=system,
            prompt=prompt,
            response=result.text,
            reasoning=result.reasoning,
        )
        return result

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
        record_decision(
            "model_decision",
            agent_id=agent_id,
            task_id=task_id,
            battery=outcome.judgments.battery,
            tier=outcome.tier,
            provider=outcome.provider,
            model=outcome.model,
            source=outcome.source,
            fallback_used=outcome.fallback_used,
            fallback_from=outcome.fallback_from,
            latency_ms=outcome.judgments.latency_ms,
            request_id=outcome.judgments.request_id,
            model_mismatch=outcome.judgments.model_mismatch,
            state_hash=outcome.state_hash,
            answers={
                name: answer.to_dict() for name, answer in outcome.judgments.answers
            },
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
