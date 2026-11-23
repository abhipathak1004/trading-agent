import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from bq.context import (
    MAX_ITEMS,
    BuiltContext,
    ContextError,
    ContextItem,
    ContextSection,
    build_context,
)
from bq.data.context import (
    context_item_for_record,
    replay_record_context,
    verify_record_citations,
)
from bq.data.records import (
    DataRecord,
    DataRecordStore,
    MissingDataRecord,
    QualityStatus,
    RecordKind,
)
from bq.infra.contexts import ContextManifestStore
from bq.infra.database import check_database, migrate
from bq.model_routing import DataClassification


class FrozenClock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


def _record(
    *,
    known_at: datetime = datetime(2026, 11, 21, 12, 0, tzinfo=UTC),
    payload: dict[str, object] | None = None,
) -> DataRecord:
    return DataRecord.create(
        kind=RecordKind.EOD_BAR,
        record_key="TEST:2026-11-20",
        source_id="synthetic-fixture",
        source_url="https://example.test/fixture",
        unit="INR/share",
        event_at=datetime(2026, 11, 20, 10, 0, tzinfo=UTC),
        published_at=datetime(2026, 11, 20, 11, 0, tzinfo=UTC),
        known_at=known_at,
        received_at=known_at - timedelta(minutes=1),
        quality_status=QualityStatus.UNASSESSED,
        payload=payload if payload is not None else {"close": 125.5, "volume": 1000},
    )


def test_as_of_lookup_returns_only_the_latest_known_append_only_revision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = DataRecordStore(path, clock)
    first = _record()
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM point_in_time_records").fetchone()[
                0
            ]
            == 0
        )
    clock.instant = first.known_at + timedelta(minutes=30)
    first_id = store.record(first)

    with pytest.raises(MissingDataRecord):
        store.record_for(
            RecordKind.EOD_BAR,
            first.record_key,
            as_of=first.known_at - timedelta(seconds=1),
        )
    assert (
        store.record_for(RecordKind.EOD_BAR, first.record_key, as_of=clock.now())
        == first
    )

    revised_at = first.known_at + timedelta(hours=1)
    revised = _record(known_at=revised_at, payload={"close": 126.0, "volume": 1200})
    clock.instant = revised_at + timedelta(minutes=1)
    revised_id = store.record(revised)
    assert first_id != revised_id
    assert (
        store.record_for(
            RecordKind.EOD_BAR,
            first.record_key,
            as_of=revised_at - timedelta(seconds=1),
        )
        == first
    )
    assert (
        store.record_for(RecordKind.EOD_BAR, first.record_key, as_of=clock.now())
        == revised
    )

    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE point_in_time_records SET quality_status = 'REJECTED' "
                "WHERE record_id = ?",
                (first_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM point_in_time_records WHERE record_id = ?", (first_id,)
            )
    check_database(path)


def test_record_payload_is_canonical_and_rejects_non_finite_values() -> None:
    record = _record(payload={"z": 1, "a": 2})
    assert record.payload_json == '{"a":2,"z":1}'
    payload = record.payload
    payload["a"] = 99
    assert record.payload["a"] == 2

    with pytest.raises(ValueError, match="finite JSON"):
        _record(payload={"bad": float("nan")})
    with pytest.raises(ValueError, match="timezone-aware"):
        _record(known_at=datetime(2026, 11, 21, 12, 0))
    with pytest.raises(ValueError, match="without credentials"):
        DataRecord.create(
            kind=RecordKind.EOD_BAR,
            record_key="TEST:2026-11-20",
            source_id="synthetic-fixture",
            source_url="https://user:pass@example.test/fixture",
            known_at=datetime(2026, 11, 21, 12, 0, tzinfo=UTC),
            received_at=datetime(2026, 11, 21, 11, 59, tzinfo=UTC),
            payload={"close": 125.5},
        )


def test_record_store_rejects_future_availability_and_detects_payload_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = DataRecordStore(path, FrozenClock())
    future = _record(known_at=datetime(2026, 11, 21, 16, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="future"):
        store.record(future)

    record = _record()
    record_id = store.record(record)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER point_in_time_records_no_update")
        connection.execute(
            "UPDATE point_in_time_records SET payload_json = ? WHERE record_id = ?",
            ('{"close":0}', record_id),
        )
    with pytest.raises(sqlite3.DatabaseError, match="integrity check failed"):
        store.record_for(
            RecordKind.EOD_BAR, record.record_key, as_of=FrozenClock().now()
        )
    with pytest.raises(sqlite3.DatabaseError, match="integrity check failed"):
        store.version_for(record_id, as_of=FrozenClock().now())


def test_late_recorded_correction_is_not_visible_before_it_was_stored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = DataRecordStore(path, clock)
    first = _record()
    store.record(first)
    recorded_at = clock.now()
    clock.instant += timedelta(hours=1)
    correction = _record(
        known_at=first.known_at + timedelta(minutes=5), payload={"close": 200}
    )
    store.record(correction)

    with pytest.raises(MissingDataRecord):
        store.record_for(
            RecordKind.EOD_BAR,
            first.record_key,
            as_of=recorded_at - timedelta(seconds=1),
        )
    assert (
        store.record_for(RecordKind.EOD_BAR, first.record_key, as_of=recorded_at)
        == first
    )
    assert (
        store.record_for(RecordKind.EOD_BAR, first.record_key, as_of=clock.now())
        == correction
    )


def test_record_rejects_same_key_revision_when_record_clock_does_not_advance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = DataRecordStore(path, clock)
    first = _record()
    store.record(first)
    correction = _record(
        known_at=first.known_at + timedelta(minutes=1), payload={"close": 200}
    )
    with pytest.raises(ValueError, match="time must advance"):
        store.record(correction)
    assert (
        store.record_for(RecordKind.EOD_BAR, first.record_key, as_of=clock.now())
        == first
    )
    clock.instant += timedelta(microseconds=1)
    store.record(correction)
    assert (
        store.record_for(RecordKind.EOD_BAR, first.record_key, as_of=clock.now())
        == correction
    )


def test_context_source_uses_retained_version_and_respects_recorded_time(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = DataRecordStore(path, clock)
    first = _record()
    first_id = store.record(first)
    first_recorded_at = clock.now()
    with pytest.raises(MissingDataRecord):
        context_item_for_record(
            store,
            first_id,
            as_of=first_recorded_at - timedelta(microseconds=1),
            section=ContextSection.FACTS,
            classification=DataClassification.PUBLIC,
            priority=0,
        )
    first_item = context_item_for_record(
        store,
        first_id,
        as_of=clock.now(),
        section=ContextSection.FACTS,
        classification=DataClassification.PUBLIC,
        priority=0,
        required=True,
    )
    assert first_item.known_at == first.known_at
    assert first_item.recorded_at == first_recorded_at
    assert first_item.version_id == f"record:{first_id}"
    assert first_item.required
    rendered = json.loads(first_item.content)
    assert rendered["payload"] == first.payload
    assert rendered["quality_status"] == "UNASSESSED"
    assert "source_url" not in rendered

    def build_record(
        item: ContextItem,
        as_of: datetime = first_recorded_at,
        additional: tuple[ContextItem, ...] = (),
        section_limits: dict[ContextSection, int] | None = None,
    ) -> BuiltContext:
        return build_context(
            (item, *additional),
            as_of=as_of,
            task_id="synthetic",
            task_version="v1",
            scope="fixture",
            model_id="model",
            prompt_version="v1",
            spec_version="v1",
            tokenizer_id="bytes",
            system="Review this record.",
            system_classification=DataClassification.PUBLIC,
            output_schema={"type": "object"},
            schema_classification=DataClassification.PUBLIC,
            section_limits=section_limits or {},
            required_sections=frozenset({ContextSection.FACTS}),
            token_limit=10000,
            output_reserve=10,
            overhead_tokens=0,
            count_tokens=lambda value: len(value.encode("utf-8")),
        )

    built = build_record(first_item)
    prompt_item = json.loads(built.prompt_json)["sections"]["facts"][0]
    assert prompt_item["content"] == first_item.content
    assert prompt_item["version_id"] == first_item.version_id
    manifests = ContextManifestStore(path, clock)
    manifests.save(built)
    assert (
        replay_record_context(manifests, store, built.manifest_hash)
        == built.prompt_json
    )
    citation = (first_item.source_id, first_item.version_id)
    verify_record_citations(manifests, store, built.manifest_hash, (citation,))
    for invalid in (
        (),
        (citation,) * (MAX_ITEMS + 1),
        (("unknown", first_item.version_id),),
        ((first_item.source_id, "record:unknown"),),
        (("", first_item.version_id),),
    ):
        with pytest.raises(ContextError, match="[Cc]itation"):
            verify_record_citations(manifests, store, built.manifest_hash, invalid)
    forged = build_record(replace(first_item, content="not the retained record"))
    manifests.save(forged)
    with pytest.raises(ContextError, match="does not match"):
        replay_record_context(manifests, store, forged.manifest_hash)
    with pytest.raises(ContextError, match="does not match"):
        verify_record_citations(manifests, store, forged.manifest_hash, (citation,))
    forged_identity = build_record(replace(first_item, source_id="data:other"))
    manifests.save(forged_identity)
    with pytest.raises(ContextError, match="does not match"):
        replay_record_context(manifests, store, forged_identity.manifest_hash)
    forged_time = build_record(
        replace(first_item, known_at=first_item.known_at - timedelta(seconds=1))
    )
    manifests.save(forged_time)
    with pytest.raises(ContextError, match="does not match"):
        replay_record_context(manifests, store, forged_time.manifest_hash)
    unsupported = build_record(replace(first_item, version_id="other-source"))
    manifests.save(unsupported)
    with pytest.raises(ContextError, match="non-record source"):
        replay_record_context(manifests, store, unsupported.manifest_hash)
    missing = build_record(replace(first_item, version_id="record:missing"))
    manifests.save(missing)
    with pytest.raises(MissingDataRecord):
        replay_record_context(manifests, store, missing.manifest_hash)
    duplicate_manifest = json.loads(built.manifest_json)
    duplicate_prompt = json.loads(built.prompt_json)
    duplicate_manifest["included"].append(duplicate_manifest["included"][0])
    duplicate_prompt["sections"]["facts"].append(
        duplicate_prompt["sections"]["facts"][0]
    )
    prompt_json = json.dumps(
        duplicate_prompt, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    duplicate_manifest["prompt_json"] = prompt_json
    duplicate_manifest["prompt_hash"] = sha256(prompt_json.encode()).hexdigest()
    manifest_json = json.dumps(
        duplicate_manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    duplicate = BuiltContext(
        prompt_json, manifest_json, sha256(manifest_json.encode()).hexdigest()
    )
    manifests.save(duplicate)
    with pytest.raises(ContextError, match="duplicate"):
        replay_record_context(manifests, store, duplicate.manifest_hash)
    earlier = first_recorded_at - timedelta(microseconds=1)
    unavailable = build_record(replace(first_item, recorded_at=earlier), earlier)
    manifests.save(unavailable)
    with pytest.raises(MissingDataRecord):
        replay_record_context(manifests, store, unavailable.manifest_hash)

    clock.instant += timedelta(hours=1)
    correction = _record(
        known_at=first.known_at + timedelta(minutes=5), payload={"close": 200}
    )
    second_id = store.record(correction)
    with pytest.raises(MissingDataRecord):
        store.version_for(second_id, as_of=first_recorded_at)
    second_item = context_item_for_record(
        store,
        second_id,
        as_of=clock.now(),
        section=ContextSection.FACTS,
        classification=DataClassification.PRIVATE,
        priority=0,
    )
    assert second_item.source_id == first_item.source_id
    assert second_item.version_id != first_item.version_id
    with_dropped = build_record(
        first_item,
        as_of=clock.now(),
        additional=(second_item,),
        section_limits={ContextSection.FACTS: 1},
    )
    manifests.save(with_dropped)
    assert len(json.loads(with_dropped.manifest_json)["dropped"]) == 1
    verify_record_citations(manifests, store, with_dropped.manifest_hash, (citation,))
    with pytest.raises(ContextError, match="not included"):
        verify_record_citations(
            manifests,
            store,
            with_dropped.manifest_hash,
            ((second_item.source_id, second_item.version_id),),
        )
    with pytest.raises(ContextError, match="not included"):
        verify_record_citations(
            manifests,
            store,
            built.manifest_hash,
            ((second_item.source_id, second_item.version_id),),
        )
    assert json.loads(second_item.content)["payload"] == correction.payload
    assert store.version_for(first_id, as_of=clock.now()).record == first
    assert (
        replay_record_context(manifests, store, built.manifest_hash)
        == built.prompt_json
    )


@pytest.mark.parametrize("record_id", ["_fixture", "-fixture"])
def test_context_source_accepts_urlsafe_generated_identifiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_id: str,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = DataRecordStore(path, clock)
    monkeypatch.setattr("bq.data.records.secrets.token_urlsafe", lambda _: record_id)
    assert store.record(_record()) == record_id
    item = context_item_for_record(
        store,
        record_id,
        as_of=clock.now(),
        section=ContextSection.FACTS,
        classification=DataClassification.PUBLIC,
        priority=0,
    )
    assert item.version_id == f"record:{record_id}"
