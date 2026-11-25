import json
from dataclasses import dataclass

from bq.context import MAX_INPUT_BYTES, SECTION_ORDER, ContextSection
from bq.model_routing import DataClassification, RoutingError, TaskPolicy, _identifier


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    policy: TaskPolicy
    prompt_version: str
    spec_version: str
    system: str
    system_classification: DataClassification
    output_schema_json: str
    schema_classification: DataClassification
    section_limits: tuple[tuple[ContextSection, int], ...] = ()
    required_sections: frozenset[ContextSection] = frozenset()
    citation_path: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.policy, TaskPolicy):
            raise RoutingError("Task policy is required.")
        _identifier(self.prompt_version)
        _identifier(self.spec_version)
        if not isinstance(self.system, str) or not self.system:
            raise RoutingError("Task system prompt is required.")
        if not isinstance(
            self.system_classification, DataClassification
        ) or not isinstance(self.schema_classification, DataClassification):
            raise RoutingError("Task classifications are required.")
        if (
            not {self.system_classification, self.schema_classification}
            <= self.policy.allowed_data
        ):
            raise RoutingError("Task policy does not allow its prompt classifications.")
        if not isinstance(self.section_limits, tuple) or any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], ContextSection)
            or isinstance(pair[1], bool)
            or not isinstance(pair[1], int)
            or pair[1] < 0
            for pair in self.section_limits
        ):
            raise RoutingError("Invalid task section limits.")
        sections = tuple(section for section, _ in self.section_limits)
        if (
            len(set(sections)) != len(sections)
            or tuple(section for section in SECTION_ORDER if section in sections)
            != sections
        ):
            raise RoutingError("Task section limits must be unique and ordered.")
        if not isinstance(self.required_sections, frozenset) or any(
            not isinstance(section, ContextSection)
            for section in self.required_sections
        ):
            raise RoutingError("Invalid required task sections.")
        if any(
            section in self.required_sections and limit == 0
            for section, limit in self.section_limits
        ):
            raise RoutingError("A required task section cannot have a zero limit.")
        if self.citation_path is not None and (
            not isinstance(self.citation_path, tuple)
            or not 1 <= len(self.citation_path) <= 16
            or any(
                not isinstance(part, str) or not part or len(part) > 128
                for part in self.citation_path
            )
        ):
            raise RoutingError("Citation path must be a bounded tuple of object keys.")
        try:
            if self.citation_path is not None:
                for part in self.citation_path:
                    part.encode("utf-8")
            schema = json.loads(self.output_schema_json)
            canonical = json.dumps(
                schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            size = len(self.system.encode("utf-8")) + len(canonical.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise RoutingError("Task output schema must be valid UTF-8 JSON.") from exc
        if (
            not isinstance(schema, dict)
            or not schema
            or canonical != self.output_schema_json
        ):
            raise RoutingError(
                "Task output schema must be a nonempty canonical JSON object."
            )
        if size > MAX_INPUT_BYTES:
            raise RoutingError("Task prompt and schema exceed the context byte limit.")
