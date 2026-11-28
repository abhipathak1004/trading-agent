from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from bq.context import ContextSection, build_context
from bq.infra.database import migrate
from bq.infra.model_attempts import ModelAttemptStore
from bq.infra.task_registry import TaskRegistry
from bq.llm_client import ModelClient
from bq.llm_gateway import LLMGateway, ProviderDisabled
from bq.llm_result import LLMResult
from bq.model_router import ModelRouter
from bq.model_routing import (
    DataClassification,
    ModelCandidate,
    ModelTier,
    TaskClass,
    TaskPolicy,
)
from bq.settings import (
    DEFAULT_AGENT_ROUTING,
    DecisionPolicyConfig,
    DecisionRoute,
    PipelineConfig,
    Provider,
    SettingsStore,
    TierConfig,
    TierName,
)
from bq.task_registry import TaskDefinition

AS_OF = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def task() -> TaskDefinition:
    return TaskDefinition(
        policy=TaskPolicy(
            task_id="confidence.review",
            version="v1",
            task_class=TaskClass.REASONING,
            allowed_data=frozenset({DataClassification.PUBLIC}),
            allowed_tiers=frozenset({ModelTier.PAID}),
        ),
        prompt_version="prompt-v1",
        spec_version="spec-v1",
        system="Review the supplied facts.",
        system_classification=DataClassification.PUBLIC,
        output_schema_json='{"properties":{"approved":{"type":"boolean"}},"required":["approved"],"type":"object"}',
        schema_classification=DataClassification.PUBLIC,
        section_limits=((ContextSection.TASK_INPUT, 100),),
        required_sections=frozenset({ContextSection.TASK_INPUT}),
    )


def _bundle() -> object:
    return build_context(
        items=(),
        as_of=AS_OF,
        task_id="confidence.review",
        task_version="v1",
        scope="test",
        model_id="paid-model",
        prompt_version="prompt-v1",
        spec_version="spec-v1",
        tokenizer_id="test-counter",
        system="Review the supplied facts.",
        system_classification=DataClassification.PUBLIC,
        output_schema={"type": "object"},
        schema_classification=DataClassification.PUBLIC,
        section_limits={ContextSection.TASK_INPUT: 100},
        required_sections=frozenset(),
        token_limit=1000,
        output_reserve=200,
        overhead_tokens=20,
        count_tokens=lambda text: len(text),
    )


def test_gateway_blocks_without_eligible_authorized_provider(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    TaskRegistry(path).save(task())
    gateway = LLMGateway(path, candidates=())

    with pytest.raises(ProviderDisabled, match="no candidates"):
        gateway.prepare(
            task_id="confidence.review",
            task_version="v1",
            data_classes=frozenset({DataClassification.PUBLIC}),
            bundle=_bundle(),
        )


def test_gateway_rejects_output_outside_registered_schema(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    gateway = LLMGateway(path)

    with pytest.raises(ValueError, match="does not match"):
        gateway.validate_output(
            task=task(),
            manifest_hash="0" * 64,
            model_id="paid-model",
            output='{"approved":"yes"}',
        )


def test_gateway_accepts_authorized_synthetic_candidate(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    definition = task()
    TaskRegistry(path).save(definition)
    candidate = ModelCandidate(
        model_id="paid-model",
        tier=ModelTier.PAID,
        available=True,
        capability_verified=True,
        terms_reviewed=True,
        supported_classes=frozenset({TaskClass.REASONING}),
        approved_data=frozenset({DataClassification.PUBLIC}),
        model_sha256=None,
        evaluations=frozenset(),
    )
    gateway = LLMGateway(path, candidates=(candidate,))
    selected_task, selected, manifest_hash = gateway.prepare(
        task_id="confidence.review",
        task_version="v1",
        data_classes=frozenset({DataClassification.PUBLIC}),
        bundle=_bundle(),
    )
    assert selected_task == definition
    assert selected == candidate
    parsed, citations = gateway.validate_output(
        task=selected_task,
        manifest_hash=manifest_hash,
        model_id=candidate.model_id,
        output='{"approved":true}',
    )
    assert parsed == {"approved": True}
    assert citations == ()


class FrozenClock:
    def now(self) -> datetime:
        return AS_OF


class FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def complete(
        self,
        *,
        model: str,
        api_key: str,
        system: str,
        prompt: str,
        timeout: float = 60.0,
        tools=None,
        tool_call=None,
        tool_result=None,
    ) -> LLMResult:
        self.calls += 1
        return LLMResult(text=self.text)


def _judge_task() -> TaskDefinition:
    return TaskDefinition(
        policy=TaskPolicy(
            task_id="analysis.judge",
            version="v1",
            task_class=TaskClass.REASONING,
            allowed_data=frozenset({DataClassification.PUBLIC}),
            allowed_tiers=frozenset({ModelTier.PAID}),
        ),
        prompt_version="prompt-v1",
        spec_version="spec-v1",
        system="Judge the case.",
        system_classification=DataClassification.PUBLIC,
        output_schema_json=(
            '{"properties":{"approved":{"type":"boolean"}},'
            '"required":["approved"],"type":"object"}'
        ),
        schema_classification=DataClassification.PUBLIC,
        section_limits=((ContextSection.TASK_INPUT, 100),),
        required_sections=frozenset({ContextSection.TASK_INPUT}),
    )


def _judge_store(tmp_path: Path) -> SettingsStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    TaskRegistry(path).save(_judge_task())
    store = SettingsStore(path, FrozenClock())
    store.save_models(
        tiers={
            TierName.DECISION: TierConfig(
                provider=Provider.TYPESAFE_JEV,
                model="jev-1.14",
                fallback_tier=TierName.FLASH,
            ),
            TierName.REASONING: TierConfig(
                provider=Provider.GEMINI,
                model="gemini-2.5-pro",
                fallback_tier=None,
            ),
            TierName.FLASH: TierConfig(
                provider=Provider.GEMINI,
                model="gemini-2.5-flash",
                fallback_tier=TierName.REASONING,
            ),
        },
        agent_routing=dict(DEFAULT_AGENT_ROUTING),
        pipeline=PipelineConfig(),
        decision_route=DecisionRoute.AUTO,
        decision_policy=DecisionPolicyConfig(),
    )
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-key-1234")
    return store


def test_router_governs_a_registered_task(tmp_path: Path) -> None:
    store = _judge_store(tmp_path)
    client = FakeClient('{"approved":true}')
    router = ModelRouter(store, clients={Provider.GEMINI: cast(ModelClient, client)})
    outcome = router.complete(
        "research_judge",
        prompt="Case facts",
        system="System",
        task_id="analysis.judge",
        task_version="v1",
    )
    assert outcome.text == '{"approved":true}'
    assert client.calls == 1
    usage = ModelAttemptStore(store.path).usage()
    assert any(
        item.task_id == "analysis.judge" and item.attempt_count == 1 for item in usage
    )


def test_router_rejects_schema_invalid_governed_output(tmp_path: Path) -> None:
    store = _judge_store(tmp_path)
    client = FakeClient('{"approved":"yes"}')
    router = ModelRouter(store, clients={Provider.GEMINI: cast(ModelClient, client)})
    with pytest.raises(ValueError, match="does not match|schema"):
        router.complete(
            "research_judge",
            prompt="Case facts",
            system="System",
            task_id="analysis.judge",
            task_version="v1",
        )
