import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from bq.model_routing import DataClassification

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
MAX_ITEMS = 256
MAX_INPUT_BYTES = 1024 * 1024


class ContextError(ValueError):
    pass


class ContextSection(StrEnum):
    REFERENCE = "reference"
    FACTS = "facts"
    NOTES = "notes"
    DOCUMENTS = "documents"
    MEMORY = "memory"
    TASK_INPUT = "task_input"


SECTION_ORDER = tuple(ContextSection)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ContextError("Context timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: object, *, sort_keys: bool) -> str:
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=sort_keys,
            separators=(",", ":"),
            allow_nan=False,
        )
        result.encode("utf-8")
        return result
    except (TypeError, ValueError) as exc:
        raise ContextError(
            "Context must contain valid UTF-8 and finite JSON data."
        ) from exc


def _canonical(value: object) -> str:
    return _json(value, sort_keys=True)


def _identifier(value: str) -> None:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ContextError("Invalid context identifier.")


def _nonnegative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContextError(f"{label} must be a nonnegative integer.")


@dataclass(frozen=True, slots=True)
class ContextItem:
    section: ContextSection
    source_id: str
    version_id: str
    known_at: datetime
    recorded_at: datetime
    content: str
    priority: int
    classification: DataClassification
    required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.section, ContextSection):
            raise ContextError("Invalid context section.")
        _identifier(self.source_id)
        _identifier(self.version_id)
        if _utc(self.known_at) > _utc(self.recorded_at):
            raise ContextError("Context cannot be recorded before it is known.")
        if not isinstance(self.content, str) or not self.content:
            raise ContextError("Context content cannot be empty.")
        _nonnegative(self.priority, "Context priority")
        if not isinstance(self.classification, DataClassification):
            raise ContextError("Invalid context data classification.")
        if not isinstance(self.required, bool):
            raise ContextError("Context required must be a boolean.")


@dataclass(frozen=True, slots=True)
class BuiltContext:
    prompt_json: str
    manifest_json: str
    manifest_hash: str


def build_context(
    items: tuple[ContextItem, ...],
    *,
    as_of: datetime,
    task_id: str,
    task_version: str,
    scope: str,
    model_id: str,
    prompt_version: str,
    spec_version: str,
    tokenizer_id: str,
    system: str,
    system_classification: DataClassification,
    output_schema: Mapping[str, object],
    schema_classification: DataClassification,
    section_limits: Mapping[ContextSection, int],
    required_sections: frozenset[ContextSection],
    token_limit: int,
    output_reserve: int,
    overhead_tokens: int,
    count_tokens: Callable[[str], int],
) -> BuiltContext:
    instant = _utc(as_of)
    for identifier in (
        task_id,
        task_version,
        scope,
        model_id,
        prompt_version,
        spec_version,
        tokenizer_id,
    ):
        _identifier(identifier)
    if not isinstance(system, str) or not system:
        raise ContextError("System prompt cannot be empty.")
    if not isinstance(system_classification, DataClassification) or not isinstance(
        schema_classification, DataClassification
    ):
        raise ContextError("System and schema classifications are required.")
    if not isinstance(output_schema, Mapping) or not output_schema:
        raise ContextError("Output schema must be a nonempty object.")
    _nonnegative(token_limit, "Token limit")
    _nonnegative(output_reserve, "Output reserve")
    _nonnegative(overhead_tokens, "Overhead")
    if token_limit == 0 or output_reserve == 0:
        raise ContextError("Token limit and output reserve must be positive.")
    if not isinstance(items, tuple) or any(
        not isinstance(item, ContextItem) for item in items
    ):
        raise ContextError("Context items must be a tuple of ContextItem values.")
    if len(items) > MAX_ITEMS:
        raise ContextError("Context exceeds the item limit.")
    if any(not isinstance(section, ContextSection) for section in section_limits):
        raise ContextError("Invalid section limit.")
    for limit in section_limits.values():
        _nonnegative(limit, "Section limit")
    if not isinstance(required_sections, frozenset) or any(
        not isinstance(section, ContextSection) for section in required_sections
    ):
        raise ContextError("Invalid required sections.")

    schema_json = _canonical(dict(output_schema))
    try:
        input_bytes = len(system.encode("utf-8")) + len(schema_json.encode("utf-8"))
        input_bytes += sum(len(item.content.encode("utf-8")) for item in items)
    except UnicodeEncodeError as exc:
        raise ContextError("Context must contain valid UTF-8.") from exc
    if input_bytes > MAX_INPUT_BYTES:
        raise ContextError("Context exceeds the input byte limit.")
    schema = json.loads(schema_json)
    seen: dict[tuple[str, str], ContextItem] = {}
    dropped: list[dict[str, object]] = []
    eligible: list[ContextItem] = []

    def drop(item: ContextItem, reason: str) -> None:
        dropped.append(
            {
                "section": item.section.value,
                "source_id": item.source_id,
                "version_id": item.version_id,
                "content_hash": sha256(item.content.encode("utf-8")).hexdigest(),
                "known_at": _timestamp(item.known_at),
                "recorded_at": _timestamp(item.recorded_at),
                "priority": item.priority,
                "classification": item.classification.value,
                "required": item.required,
                "reason": reason,
            }
        )

    for item in items:
        identity = (item.source_id, item.version_id)
        if identity in seen:
            if item != seen[identity]:
                raise ContextError(
                    "Conflicting content or metadata for a context version."
                )
            drop(item, "duplicate")
            continue
        seen[identity] = item
        if _utc(item.known_at) > instant or _utc(item.recorded_at) > instant:
            if item.required:
                raise ContextError(
                    "Required context was unavailable at the requested time."
                )
            drop(item, "after_as_of")
            continue
        eligible.append(item)

    def sort_key(item: ContextItem) -> tuple[int, int, str, str]:
        return (
            SECTION_ORDER.index(item.section),
            -item.priority,
            item.source_id,
            item.version_id,
        )

    eligible.sort(key=sort_key)
    selected: list[ContextItem] = []
    for section in SECTION_ORDER:
        candidates = [item for item in eligible if item.section is section]
        limit = section_limits.get(section, len(candidates))
        required = [item for item in candidates if item.required]
        if len(required) > limit:
            raise ContextError(f"Required context exceeds the {section} section limit.")
        optional_slots = limit - len(required)
        optional = [item for item in candidates if not item.required]
        selected.extend(required + optional[:optional_slots])
        for item in optional[optional_slots:]:
            drop(item, "section_limit")
    selected.sort(key=sort_key)
    if any(
        not any(item.section is section for item in selected)
        for section in required_sections
    ):
        raise ContextError("Required context section is unavailable.")

    def render() -> str:
        return _json(
            {
                "system": system,
                "sections": {
                    section.value: [
                        {
                            "source_id": item.source_id,
                            "version_id": item.version_id,
                            "content": item.content,
                        }
                        for item in selected
                        if item.section is section
                    ]
                    for section in SECTION_ORDER
                },
                "output_schema": schema,
            },
            sort_keys=False,
        )

    while True:
        prompt = render()
        token_count = count_tokens(prompt)
        _nonnegative(token_count, "Token count")
        if token_count + overhead_tokens + output_reserve <= token_limit:
            break
        removable = next(
            (
                item
                for item in reversed(selected)
                if not item.required
                and (
                    item.section not in required_sections
                    or sum(other.section is item.section for other in selected) > 1
                )
            ),
            None,
        )
        if removable is None:
            raise ContextError("Required context exceeds the token budget.")
        selected.remove(removable)
        drop(removable, "token_budget")

    manifest = _canonical(
        {
            "as_of": _timestamp(instant),
            "task_id": task_id,
            "task_version": task_version,
            "scope": scope,
            "model_id": model_id,
            "prompt_version": prompt_version,
            "spec_version": spec_version,
            "tokenizer_id": tokenizer_id,
            "prompt_json": prompt,
            "prompt_hash": sha256(prompt.encode("utf-8")).hexdigest(),
            "data_classes": sorted(
                classification.value
                for classification in {
                    system_classification,
                    schema_classification,
                    *(item.classification for item in selected),
                }
            ),
            "system_classification": system_classification.value,
            "schema_classification": schema_classification.value,
            "included": [
                {
                    "section": item.section.value,
                    "source_id": item.source_id,
                    "version_id": item.version_id,
                    "known_at": _timestamp(item.known_at),
                    "recorded_at": _timestamp(item.recorded_at),
                    "content_hash": sha256(item.content.encode("utf-8")).hexdigest(),
                    "priority": item.priority,
                    "classification": item.classification.value,
                    "required": item.required,
                }
                for item in selected
            ],
            "dropped": sorted(
                dropped,
                key=lambda item: (
                    str(item["source_id"]),
                    str(item["version_id"]),
                    str(item["reason"]),
                ),
            ),
            "section_limits": {
                section.value: section_limits[section]
                for section in SECTION_ORDER
                if section in section_limits
            },
            "required_sections": [
                section.value
                for section in SECTION_ORDER
                if section in required_sections
            ],
            "token_limit": token_limit,
            "input_tokens": token_count,
            "output_reserve": output_reserve,
            "overhead_tokens": overhead_tokens,
        }
    )
    return BuiltContext(prompt, manifest, sha256(manifest.encode("utf-8")).hexdigest())
