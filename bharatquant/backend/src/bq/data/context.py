import json
from datetime import datetime
from hashlib import sha256

from bq.context import (
    MAX_ITEMS,
    SECTION_ORDER,
    ContextError,
    ContextItem,
    ContextSection,
)
from bq.data.records import DataRecordStore
from bq.infra.contexts import ContextManifestStore
from bq.model_routing import DataClassification


def context_item_for_record(
    store: DataRecordStore,
    record_id: str,
    *,
    as_of: datetime,
    section: ContextSection,
    classification: DataClassification,
    priority: int,
    required: bool = False,
) -> ContextItem:
    version = store.version_for(record_id, as_of=as_of)
    record = version.record
    identity = f"{record.kind.value}\n{record.record_key}"
    content = json.dumps(
        {
            "kind": record.kind.value,
            "record_key": record.record_key,
            "source_id": record.source_id,
            "unit": record.unit,
            "event_at": record.event_at.isoformat() if record.event_at else None,
            "quality_status": record.quality_status.value,
            "payload": record.payload,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return ContextItem(
        section=section,
        source_id=f"data:{sha256(identity.encode('utf-8')).hexdigest()}",
        version_id=f"record:{version.record_id}",
        known_at=record.known_at,
        recorded_at=version.recorded_at,
        content=content,
        priority=priority,
        classification=classification,
        required=required,
    )


def replay_record_context(
    manifests: ContextManifestStore, records: DataRecordStore, manifest_hash: str
) -> str:
    stored = manifests.load(manifest_hash)
    manifest = json.loads(stored.manifest_json)
    prompt = json.loads(stored.prompt_json)
    try:
        as_of = datetime.fromisoformat(manifest["as_of"].replace("Z", "+00:00"))
        included = manifest["included"]
        sections = prompt["sections"]
        if (
            not isinstance(included, list)
            or not isinstance(sections, dict)
            or list(sections) != [section.value for section in SECTION_ORDER]
            or list(prompt) != ["system", "sections", "output_schema"]
        ):
            raise ContextError("Invalid record-backed context structure.")
        rendered: dict[str, list[dict[str, str]]] = {
            section.value: [] for section in SECTION_ORDER
        }
        seen: set[tuple[str, str]] = set()
        index = 0
        for section in SECTION_ORDER:
            entries = sections[section.value]
            if not isinstance(entries, list):
                raise ContextError("Invalid record-backed context section.")
            for entry in entries:
                if index >= len(included):
                    raise ContextError(
                        "Record-backed context provenance is incomplete."
                    )
                provenance = included[index]
                index += 1
                version_id = provenance["version_id"]
                if not isinstance(version_id, str) or not version_id.startswith(
                    "record:"
                ):
                    raise ContextError("Context includes a non-record source.")
                item = context_item_for_record(
                    records,
                    version_id.removeprefix("record:"),
                    as_of=as_of,
                    section=section,
                    classification=DataClassification(provenance["classification"]),
                    priority=provenance["priority"],
                    required=provenance["required"],
                )
                identity = (item.source_id, item.version_id)
                if identity in seen:
                    raise ContextError("Record-backed context contains a duplicate.")
                seen.add(identity)
                if (
                    provenance["section"] != section.value
                    or provenance["source_id"] != item.source_id
                    or datetime.fromisoformat(
                        provenance["known_at"].replace("Z", "+00:00")
                    )
                    != item.known_at
                    or datetime.fromisoformat(
                        provenance["recorded_at"].replace("Z", "+00:00")
                    )
                    != item.recorded_at
                    or provenance["content_hash"]
                    != sha256(item.content.encode("utf-8")).hexdigest()
                    or entry
                    != {
                        "source_id": item.source_id,
                        "version_id": item.version_id,
                        "content": item.content,
                    }
                ):
                    raise ContextError(
                        "Record-backed context does not match its source."
                    )
                rendered[section.value].append(
                    {
                        "source_id": item.source_id,
                        "version_id": item.version_id,
                        "content": item.content,
                    }
                )
        if index != len(included):
            raise ContextError("Record-backed context provenance is incomplete.")
        replay = json.dumps(
            {
                "system": prompt["system"],
                "sections": rendered,
                "output_schema": prompt["output_schema"],
            },
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if replay != stored.prompt_json:
            raise ContextError("Record-backed context prompt is not canonical.")
        return replay
    except ContextError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ContextError("Invalid record-backed context metadata.") from exc


def verify_record_citations(
    manifests: ContextManifestStore,
    records: DataRecordStore,
    manifest_hash: str,
    citations: tuple[tuple[str, str], ...],
) -> None:
    if (
        not isinstance(citations, tuple)
        or not 1 <= len(citations) <= MAX_ITEMS
        or any(
            not isinstance(citation, tuple)
            or len(citation) != 2
            or any(not isinstance(value, str) or not value for value in citation)
            for citation in citations
        )
    ):
        raise ContextError("Citations must be a nonempty bounded tuple of identities.")
    prompt = json.loads(replay_record_context(manifests, records, manifest_hash))
    available = {
        (entry["source_id"], entry["version_id"])
        for entries in prompt["sections"].values()
        for entry in entries
    }
    if any(citation not in available for citation in citations):
        raise ContextError("A citation is not included in the replayed context.")
