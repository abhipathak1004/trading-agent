import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bq.context import BuiltContext, ContextItem, ContextSection, build_context
from bq.core import Clock, SystemClock, utc
from bq.data.records import DataRecordStore
from bq.infra.contexts import ContextManifestStore
from bq.infra.model_attempts import AttemptOutcome, ModelAttemptStore
from bq.infra.task_registry import TaskRegistry
from bq.model_output import extract_record_citations, validate_model_output
from bq.model_routing import (
    DataClassification,
    ModelCandidate,
    RouteUnavailable,
    RoutingError,
    select_model,
)
from bq.settings import SettingsStore
from bq.task_registry import TaskDefinition


class GatewayError(ValueError):
    pass


class ProviderDisabled(GatewayError):
    pass


@dataclass(frozen=True, slots=True)
class GatewayResult:
    attempt_id: str
    manifest_hash: str
    model_id: str
    parsed_output: object


class LLMGateway:
    """Authoritative offline boundary for routing and validating model calls."""

    def __init__(
        self,
        path: Path,
        *,
        candidates: tuple[ModelCandidate, ...] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.path = path
        self._explicit_candidates = candidates
        self.clock = clock or SystemClock()

    def _candidates(self) -> tuple[ModelCandidate, ...]:
        """Explicit candidates win; otherwise derive one from Settings.

        Removing the "no provider connected" gate means an owner who configured a
        provider, model and key gets a real route. The routing gate still runs —
        we satisfy it from configuration instead of skipping it.
        """
        if self._explicit_candidates is not None:
            return self._explicit_candidates
        from bq.llm_client import LLMNotConfigured
        from bq.llm_service import candidate_for

        try:
            return (candidate_for(SettingsStore(self.path).load()),)
        except LLMNotConfigured:
            return ()

    def prepare(
        self,
        *,
        task_id: str,
        task_version: str,
        data_classes: frozenset[DataClassification],
        bundle: BuiltContext,
        model_id: str | None = None,
        candidates: tuple[ModelCandidate, ...] | None = None,
    ) -> tuple[TaskDefinition, ModelCandidate, str]:
        try:
            task = TaskRegistry(self.path, self.clock).load(task_id, task_version)
        except Exception as exc:
            raise GatewayError("Task definition is unavailable.") from exc

        try:
            selected = select_model(
                task.policy,
                data_classes=data_classes,
                candidates=self._candidates() if candidates is None else candidates,
            )
        except RouteUnavailable as exc:
            raise ProviderDisabled(str(exc)) from exc
        except RoutingError as exc:
            raise GatewayError(str(exc)) from exc

        if model_id is not None and model_id != selected.model_id:
            raise GatewayError("Requested model is not eligible for this task.")

        manifest_store = ContextManifestStore(self.path, self.clock)
        manifest_store.save(bundle)
        return task, selected, bundle.manifest_hash

    def validate_output(
        self,
        *,
        task: TaskDefinition,
        manifest_hash: str,
        model_id: str,
        output: str,
    ) -> tuple[object, tuple[tuple[str, str], ...]]:
        parsed = validate_model_output(task, output)
        citations = (
            extract_record_citations(parsed, task=task)
            if task.citation_path is not None
            else ()
        )
        if task.citation_path is not None:
            context = ContextManifestStore(self.path, self.clock).load(manifest_hash)
            manifest = json.loads(context.manifest_json)
            as_of = datetime.fromisoformat(
                str(manifest["as_of"]).replace("Z", "+00:00")
            )
            records = DataRecordStore(self.path)
            for source_id, version_id in citations:
                try:
                    version = records.version_for(version_id, as_of=as_of)
                except Exception as exc:
                    raise GatewayError(
                        f"Citation version {version_id} is unavailable."
                    ) from exc
                if version.record.source_id != source_id:
                    raise GatewayError(
                        f"Citation source {source_id} does not match retained version."
                    )
        return parsed, citations

    def complete(
        self,
        *,
        task_id: str,
        task_version: str,
        manifest_hash: str,
        model_id: str,
        outcome: AttemptOutcome,
    ) -> str:
        attempt_id = secrets.token_urlsafe(18)
        attempts = ModelAttemptStore(self.path, self.clock)
        attempts.start(
            attempt_id,
            task_id=task_id,
            task_version=task_version,
            model_id=model_id,
            manifest_hash=manifest_hash,
        )
        attempts.finish(attempt_id, outcome)
        return attempt_id


def build_prompt_context(
    task: TaskDefinition,
    *,
    prompt: str,
    system: str,
    model_id: str,
    scope: str,
    as_of: datetime,
) -> BuiltContext:
    """Build a manifest for a governed call from a plain prompt and system.

    The prompt is carried as the single required ``TASK_INPUT`` item, so the
    manifest records exactly what the model was shown. A registered task whose
    ``required_sections`` name sections the caller did not supply still fails
    closed, which is the intended contract.
    """
    instant = utc(as_of)
    effective_system = system or task.system
    item = ContextItem(
        section=ContextSection.TASK_INPUT,
        source_id="prompt",
        version_id="v1",
        known_at=instant,
        recorded_at=instant,
        content=prompt,
        priority=0,
        classification=task.system_classification,
        required=True,
    )
    token_limit = len(prompt) + len(effective_system) + 4096
    return build_context(
        items=(item,),
        as_of=instant,
        task_id=task.policy.task_id,
        task_version=task.policy.version,
        scope=scope,
        model_id=model_id,
        prompt_version=task.prompt_version,
        spec_version=task.spec_version,
        tokenizer_id="chars",
        system=effective_system,
        system_classification=task.system_classification,
        output_schema=json.loads(task.output_schema_json),
        schema_classification=task.schema_classification,
        section_limits=dict(task.section_limits),
        required_sections=task.required_sections,
        token_limit=token_limit,
        output_reserve=1024,
        overhead_tokens=0,
        count_tokens=len,
    )


__all__ = [
    "GatewayError",
    "GatewayResult",
    "LLMGateway",
    "ProviderDisabled",
    "build_prompt_context",
]
