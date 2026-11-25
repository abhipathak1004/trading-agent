from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.context import ContextSection, build_context
from bq.infra.database import migrate
from bq.infra.task_registry import TaskRegistry
from bq.llm_gateway import LLMGateway, ProviderDisabled
from bq.model_routing import (
    DataClassification,
    ModelCandidate,
    ModelTier,
    TaskClass,
    TaskPolicy,
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
