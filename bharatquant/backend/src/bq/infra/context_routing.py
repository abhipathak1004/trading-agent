import json
from hashlib import sha256

from bq.context import SECTION_ORDER
from bq.data.context import replay_record_context, verify_record_citations
from bq.data.records import DataRecordStore
from bq.infra.contexts import ContextManifestStore
from bq.infra.task_registry import TaskRegistry
from bq.model_output import extract_record_citations, validate_model_output
from bq.model_routing import (
    DataClassification,
    ModelCandidate,
    RouteUnavailable,
    RoutingError,
    TaskPolicy,
    select_model,
)


def select_for_context(
    task: TaskPolicy,
    *,
    manifest_hash: str,
    store: ContextManifestStore,
    candidates: tuple[ModelCandidate, ...],
) -> ModelCandidate:
    if not isinstance(task, TaskPolicy):
        raise RoutingError("Task policy is required.")
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, ModelCandidate) for candidate in candidates
    ):
        raise RoutingError("Model candidates must be a tuple.")
    if len({candidate.model_id for candidate in candidates}) != len(candidates):
        raise RoutingError("Model candidate identifiers must be unique.")
    context = store.load(manifest_hash)
    manifest = json.loads(context.manifest_json)
    try:
        if (
            manifest["task_id"] != task.task_id
            or manifest["task_version"] != task.version
        ):
            raise RoutingError("Context task does not match the task policy.")
        prompt = json.loads(context.prompt_json)
        if (
            not isinstance(prompt, dict)
            or list(prompt) != ["system", "sections", "output_schema"]
            or not isinstance(prompt["system"], str)
            or not isinstance(prompt["output_schema"], dict)
            or not isinstance(prompt["sections"], dict)
            or list(prompt["sections"]) != [section.value for section in SECTION_ORDER]
        ):
            raise RoutingError("Invalid rendered context.")
        included = manifest["included"]
        if not isinstance(included, list):
            raise RoutingError("Invalid context provenance.")
        classes = {
            DataClassification(manifest["system_classification"]),
            DataClassification(manifest["schema_classification"]),
        }
        rendered: list[tuple[str, object]] = []
        for section in SECTION_ORDER:
            entries = prompt["sections"][section.value]
            if not isinstance(entries, list):
                raise RoutingError("Invalid rendered context section.")
            rendered.extend((section.value, entry) for entry in entries)
        if len(rendered) != len(included):
            raise RoutingError("Context provenance does not match its prompt.")
        for (section_name, entry), provenance in zip(rendered, included, strict=True):
            if (
                not isinstance(entry, dict)
                or set(entry) != {"source_id", "version_id", "content"}
                or not isinstance(entry["content"], str)
                or not isinstance(provenance, dict)
                or provenance.get("section") != section_name
                or provenance.get("source_id") != entry["source_id"]
                or provenance.get("version_id") != entry["version_id"]
                or provenance.get("content_hash")
                != sha256(entry["content"].encode("utf-8")).hexdigest()
            ):
                raise RoutingError("Context provenance does not match its prompt.")
            classes.add(DataClassification(provenance["classification"]))
        if manifest["data_classes"] != sorted(item.value for item in classes):
            raise RoutingError("Context data classifications are inconsistent.")
        model_id = manifest["model_id"]
        if not isinstance(model_id, str):
            raise RoutingError("Context model is invalid.")
    except RoutingError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise RoutingError("Context lacks valid routing metadata.") from exc
    matching = tuple(
        candidate for candidate in candidates if candidate.model_id == model_id
    )
    if not matching:
        raise RouteUnavailable(
            "The context model is not among the candidates; rebuild for a different model."
        )
    return select_model(task, data_classes=frozenset(classes), candidates=matching)


def select_for_registered_context(
    task_id: str,
    task_version: str,
    *,
    registry: TaskRegistry,
    manifest_hash: str,
    store: ContextManifestStore,
    candidates: tuple[ModelCandidate, ...],
) -> ModelCandidate:
    definition = registry.load(task_id, task_version)
    context = store.load(manifest_hash)
    manifest = json.loads(context.manifest_json)
    try:
        prompt = json.loads(context.prompt_json)
        schema = json.dumps(
            prompt["output_schema"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        limits = {section.value: limit for section, limit in definition.section_limits}
        required = [
            section.value
            for section in SECTION_ORDER
            if section in definition.required_sections
        ]
        if (
            manifest["prompt_version"] != definition.prompt_version
            or manifest["spec_version"] != definition.spec_version
            or manifest["system_classification"]
            != definition.system_classification.value
            or manifest["schema_classification"]
            != definition.schema_classification.value
            or prompt["system"] != definition.system
            or schema != definition.output_schema_json
            or manifest["section_limits"] != limits
            or manifest["required_sections"] != required
        ):
            raise RoutingError("Context does not match the registered task definition.")
        sections = prompt["sections"]
        if not isinstance(sections, dict) or any(
            not isinstance(sections.get(section.value), list)
            or (
                section.value in limits
                and len(sections[section.value]) > limits[section.value]
            )
            or (section in definition.required_sections and not sections[section.value])
            for section in SECTION_ORDER
        ):
            raise RoutingError("Context violates registered task section requirements.")
    except RoutingError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise RoutingError("Context lacks valid task metadata.") from exc
    return select_for_context(
        definition.policy,
        manifest_hash=manifest_hash,
        store=store,
        candidates=candidates,
    )


def validate_registered_record_output(
    task_id: str,
    task_version: str,
    *,
    registry: TaskRegistry,
    manifest_hash: str,
    store: ContextManifestStore,
    records: DataRecordStore,
    candidates: tuple[ModelCandidate, ...],
    output: str,
) -> object:
    definition = registry.load(task_id, task_version)
    select_for_registered_context(
        task_id,
        task_version,
        registry=registry,
        manifest_hash=manifest_hash,
        store=store,
        candidates=candidates,
    )
    replay_record_context(store, records, manifest_hash)
    parsed = validate_model_output(definition, output)
    if definition.citation_path is not None:
        citations = extract_record_citations(parsed, task=definition)
        verify_record_citations(store, records, manifest_hash, citations)
    return parsed
