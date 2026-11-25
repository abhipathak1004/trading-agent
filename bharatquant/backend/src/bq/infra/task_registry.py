import json
import sqlite3
from contextlib import closing
from datetime import UTC
from hashlib import sha256
from pathlib import Path

from bq.context import SECTION_ORDER, ContextSection
from bq.core import Clock, SystemClock
from bq.infra.audit import append_event
from bq.model_routing import (
    DataClassification,
    ModelTier,
    RoutingError,
    TaskClass,
    TaskPolicy,
    _identifier,
)
from bq.task_registry import TaskDefinition


class TaskDefinitionConflict(RoutingError):
    pass


class TaskDefinitionNotFound(LookupError):
    pass


def _serialize(definition: TaskDefinition) -> str:
    policy = definition.policy
    citation_spec = (
        {"citation_path": list(definition.citation_path)}
        if definition.citation_path is not None
        else {}
    )
    return json.dumps(
        {
            "task_id": policy.task_id,
            "version": policy.version,
            "task_class": policy.task_class.value,
            "allowed_data": sorted(item.value for item in policy.allowed_data),
            "allowed_tiers": sorted(item.value for item in policy.allowed_tiers),
            "prompt_version": definition.prompt_version,
            "spec_version": definition.spec_version,
            "system": definition.system,
            "system_classification": definition.system_classification.value,
            "output_schema_json": definition.output_schema_json,
            "schema_classification": definition.schema_classification.value,
            "section_limits": {
                section.value: limit for section, limit in definition.section_limits
            },
            "required_sections": [
                section.value
                for section in SECTION_ORDER
                if section in definition.required_sections
            ],
            **citation_spec,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _deserialize(payload: str) -> TaskDefinition:
    try:
        data = json.loads(payload)
        definition = TaskDefinition(
            policy=TaskPolicy(
                data["task_id"],
                data["version"],
                TaskClass(data["task_class"]),
                frozenset(DataClassification(item) for item in data["allowed_data"]),
                frozenset(ModelTier(item) for item in data["allowed_tiers"]),
            ),
            prompt_version=data["prompt_version"],
            spec_version=data["spec_version"],
            system=data["system"],
            system_classification=DataClassification(data["system_classification"]),
            output_schema_json=data["output_schema_json"],
            schema_classification=DataClassification(data["schema_classification"]),
            section_limits=tuple(
                (section, data["section_limits"][section.value])
                for section in SECTION_ORDER
                if section.value in data["section_limits"]
            ),
            required_sections=frozenset(
                ContextSection(section) for section in data["required_sections"]
            ),
            citation_path=(
                tuple(data["citation_path"]) if "citation_path" in data else None
            ),
        )
        if _serialize(definition) != payload:
            raise ValueError("Definition is not canonical.")
        return definition
    except (KeyError, TypeError, ValueError) as exc:
        raise sqlite3.DatabaseError("Invalid stored task definition.") from exc


class TaskRegistry:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )

    def save(self, definition: TaskDefinition) -> None:
        if not isinstance(definition, TaskDefinition):
            raise RoutingError("Task definition is required.")
        payload = _serialize(definition)
        digest = sha256(payload.encode("utf-8")).hexdigest()
        policy = definition.policy
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                row = connection.execute(
                    "SELECT definition_json, definition_hash FROM task_definitions "
                    "WHERE task_id = ? AND task_version = ?",
                    (policy.task_id, policy.version),
                ).fetchone()
                if row is not None:
                    if row[1] != sha256(row[0].encode("utf-8")).hexdigest():
                        raise sqlite3.DatabaseError(
                            "Task definition integrity check failed."
                        )
                    if row[0] != payload:
                        raise TaskDefinitionConflict(
                            "Task version already has another definition."
                        )
                    return
                now = self.clock.now()
                if now.utcoffset() is None:
                    raise RoutingError("Task registry clock must be timezone-aware.")
                now = now.astimezone(UTC)
                connection.execute(
                    "INSERT INTO task_definitions VALUES (?, ?, ?, ?, ?)",
                    (
                        policy.task_id,
                        policy.version,
                        now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                        payload,
                        digest,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="llm.task_definition.saved",
                    object_type="task_definition",
                    object_id=policy.task_id,
                    config_version=policy.version,
                    payload={"definition_hash": digest},
                )

    def load(self, task_id: str, version: str) -> TaskDefinition:
        _identifier(task_id)
        _identifier(version)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT definition_json, definition_hash FROM task_definitions "
                "WHERE task_id = ? AND task_version = ?",
                (task_id, version),
            ).fetchone()
        if row is None:
            raise TaskDefinitionNotFound(
                f"Task definition {task_id}/{version} not found."
            )
        if sha256(row[0].encode("utf-8")).hexdigest() != row[1]:
            raise sqlite3.DatabaseError("Task definition integrity check failed.")
        definition = _deserialize(row[0])
        if definition.policy.task_id != task_id or definition.policy.version != version:
            raise sqlite3.DatabaseError(
                "Stored task definition identity does not match."
            )
        return definition
