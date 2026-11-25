import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bq.context import BuiltContext
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
    ) -> None:
        self.path = path
        self._explicit_candidates = candidates

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
    ) -> tuple[TaskDefinition, ModelCandidate, str]:
        try:
            task = TaskRegistry(self.path).load(task_id, task_version)
        except Exception as exc:
            raise GatewayError("Task definition is unavailable.") from exc

        try:
            selected = select_model(
                task.policy,
                data_classes=data_classes,
                candidates=self._candidates(),
            )
        except RouteUnavailable as exc:
            raise ProviderDisabled(str(exc)) from exc
        except RoutingError as exc:
            raise GatewayError(str(exc)) from exc

        if model_id is not None and model_id != selected.model_id:
            raise GatewayError("Requested model is not eligible for this task.")

        manifest_store = ContextManifestStore(self.path)
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
            context = ContextManifestStore(self.path).load(manifest_hash)
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
        attempts = ModelAttemptStore(self.path)
        attempts.start(
            attempt_id,
            task_id=task_id,
            task_version=task_version,
            model_id=model_id,
            manifest_hash=manifest_hash,
        )
        attempts.finish(attempt_id, outcome)
        return attempt_id
