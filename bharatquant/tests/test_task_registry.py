import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from bq.context import (
    BuiltContext,
    ContextError,
    ContextItem,
    ContextSection,
    build_context,
)
from bq.data.context import context_item_for_record
from bq.data.records import DataRecord, DataRecordStore, RecordKind
from bq.infra import database
from bq.infra.context_routing import (
    select_for_registered_context,
    validate_registered_record_output,
)
from bq.infra.contexts import ContextManifestStore
from bq.infra.task_registry import (
    TaskDefinitionConflict,
    TaskDefinitionNotFound,
    TaskRegistry,
)
from bq.model_output import ModelOutputError
from bq.model_routing import (
    DataClassification,
    ModelCandidate,
    ModelTier,
    RoutingError,
    TaskClass,
    TaskPolicy,
)
from bq.task_registry import TaskDefinition

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
PUBLIC = DataClassification.PUBLIC


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def definition() -> TaskDefinition:
    return TaskDefinition(
        policy=TaskPolicy(
            "task",
            "v1",
            TaskClass.REASONING,
            frozenset(DataClassification),
            frozenset(ModelTier),
        ),
        prompt_version="prompt-v1",
        spec_version="spec-v1",
        system="Use the supplied evidence.",
        system_classification=PUBLIC,
        output_schema_json='{"type":"object"}',
        schema_classification=PUBLIC,
    )


def context(
    *,
    system: str = "Use the supplied evidence.",
    schema: dict[str, object] | None = None,
    prompt_version: str = "prompt-v1",
    spec_version: str = "spec-v1",
    system_classification: DataClassification = PUBLIC,
    schema_classification: DataClassification = PUBLIC,
    task_version: str = "v1",
    items: tuple[ContextItem, ...] = (),
    section_limits: dict[ContextSection, int] | None = None,
    required_sections: frozenset[ContextSection] = frozenset(),
) -> BuiltContext:
    return build_context(
        items,
        as_of=NOW - timedelta(seconds=1),
        task_id="task",
        task_version=task_version,
        scope="fixture",
        model_id="model",
        prompt_version=prompt_version,
        spec_version=spec_version,
        tokenizer_id="bytes",
        system=system,
        system_classification=system_classification,
        output_schema=schema if schema is not None else {"type": "object"},
        schema_classification=schema_classification,
        section_limits=section_limits if section_limits is not None else {},
        required_sections=required_sections,
        token_limit=10000,
        output_reserve=10,
        overhead_tokens=0,
        count_tokens=lambda value: len(value.encode("utf-8")),
    )


def test_task_definition_is_versioned_audited_and_append_only(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    database.migrate(path, create=True)
    registry = TaskRegistry(path, FrozenClock())
    task = definition()
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(registry.save, (task, task)))
    assert registry.load("task", "v1") == task
    with pytest.raises(TaskDefinitionNotFound):
        registry.load("task", "missing")
    with pytest.raises(TaskDefinitionConflict):
        registry.save(replace(task, system="Different prompt."))
    second = replace(task, policy=replace(task.policy, version="v2"))
    registry.save(second)
    assert registry.load("task", "v2") == second
    with closing(sqlite3.connect(path)) as connection, connection:
        stored = connection.execute(
            "SELECT definition_json FROM task_definitions WHERE task_version = 'v1'"
        ).fetchone()
        assert "citation_path" not in json.loads(stored[0])
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'llm.task_definition.saved'"
        ).fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM task_definitions")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE task_definitions SET definition_json = '{}'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "INSERT OR REPLACE INTO task_definitions SELECT * FROM task_definitions"
            )
    database.check_database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER task_definitions_no_update")
        connection.execute(
            "UPDATE task_definitions SET definition_json = '{}' WHERE task_version = 'v1'"
        )
    with pytest.raises(sqlite3.DatabaseError, match="integrity"):
        registry.load("task", "v1")


def test_offline_registered_output_checks_routing_replay_and_schema(
    tmp_path: Path,
) -> None:
    class EarlierClock:
        def now(self) -> datetime:
            return NOW - timedelta(seconds=2)

    path = tmp_path / "bq.db"
    database.migrate(path, create=True)
    registry = TaskRegistry(path, FrozenClock())
    registry.save(definition())
    records = DataRecordStore(path, EarlierClock())
    known_at = NOW - timedelta(seconds=3)
    record_id = records.record(
        DataRecord.create(
            kind=RecordKind.MARKET_STATISTIC,
            record_key="fixture",
            source_id="fixture",
            source_url="https://example.test/fixture",
            known_at=known_at,
            received_at=known_at,
            payload={"value": 1},
        )
    )
    item = context_item_for_record(
        records,
        record_id,
        as_of=NOW - timedelta(seconds=1),
        section=ContextSection.FACTS,
        classification=PUBLIC,
        priority=0,
    )
    manifests = ContextManifestStore(path, FrozenClock())
    valid = context(items=(item,))
    manifests.save(valid)
    candidate = ModelCandidate(
        "model",
        ModelTier.FREE,
        True,
        True,
        True,
        frozenset(TaskClass),
        frozenset(DataClassification),
        None,
        frozenset(),
    )

    def validate(manifest_hash: str, output: str) -> object:
        return validate_registered_record_output(
            "task",
            "v1",
            registry=registry,
            manifest_hash=manifest_hash,
            store=manifests,
            records=records,
            candidates=(candidate,),
            output=output,
        )

    assert validate(valid.manifest_hash, '{"value":1}') == {"value": 1}
    with pytest.raises(ModelOutputError, match="does not match"):
        validate(valid.manifest_hash, "[]")
    wrong_schema = context(schema={"type": "array"}, items=(item,))
    manifests.save(wrong_schema)
    with pytest.raises(RoutingError, match="does not match"):
        validate(wrong_schema.manifest_hash, "[]")
    with pytest.raises(RoutingError, match="unavailable"):
        validate_registered_record_output(
            "task",
            "v1",
            registry=registry,
            manifest_hash=valid.manifest_hash,
            store=manifests,
            records=records,
            candidates=(replace(candidate, available=False),),
            output="{}",
        )
    forged = context(items=(replace(item, content="forged"),))
    manifests.save(forged)
    with pytest.raises(ValueError, match="does not match"):
        validate(forged.manifest_hash, "{}")

    cited_task = replace(
        definition(),
        policy=replace(definition().policy, version="v2"),
        citation_path=("evidence", "citations"),
    )
    registry.save(cited_task)
    assert registry.load("task", "v2") == cited_task
    with pytest.raises(TaskDefinitionConflict):
        registry.save(replace(cited_task, citation_path=("citations",)))
    cited_context = context(task_version="v2", items=(item,))
    manifests.save(cited_context)

    def validate_cited(output: object) -> object:
        return validate_registered_record_output(
            "task",
            "v2",
            registry=registry,
            manifest_hash=cited_context.manifest_hash,
            store=manifests,
            records=records,
            candidates=(candidate,),
            output=json.dumps(output),
        )

    citation = {"source_id": item.source_id, "version_id": item.version_id}
    output = {"evidence": {"citations": [citation]}}
    assert validate_cited(output) == output
    invalid_outputs: tuple[object, ...] = (
        {},
        {"evidence": {}},
        {"evidence": {"citations": []}},
        {"evidence": {"citations": [citation, citation]}},
        {"evidence": {"citations": [{"source_id": item.source_id}]}},
        {"evidence": {"citations": [{**citation, "extra": "unexpected"}]}},
        {"evidence": {"citations": [{"source_id": 1, "version_id": item.version_id}]}},
    )
    for invalid in invalid_outputs:
        with pytest.raises(ModelOutputError):
            validate_cited(invalid)
    with pytest.raises(ContextError, match="not included"):
        validate_cited(
            {"evidence": {"citations": [{**citation, "version_id": "record:other"}]}}
        )


def test_task_definition_rejects_invalid_schema_and_classifications() -> None:
    task = definition()
    for schema in ("{}", '{"type": "object"}', '{"x":NaN}', "[]"):
        with pytest.raises(RoutingError, match="schema"):
            replace(task, output_schema_json=schema)
    with pytest.raises(RoutingError, match="classifications"):
        replace(
            task,
            system_classification=DataClassification.PRIVATE,
            policy=replace(task.policy, allowed_data=frozenset({PUBLIC})),
        )
    with pytest.raises(RoutingError, match="zero limit"):
        replace(
            task,
            section_limits=((ContextSection.FACTS, 0),),
            required_sections=frozenset({ContextSection.FACTS}),
        )
    with pytest.raises(RoutingError, match="unique"):
        replace(
            task,
            section_limits=((ContextSection.FACTS, 1), (ContextSection.FACTS, 1)),
        )
    for path in ((), ("",), ("a" * 129,), ("part",) * 17, ("\ud800",)):
        with pytest.raises(RoutingError):
            replace(task, citation_path=path)


def test_registered_context_must_match_exact_task_definition(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    database.migrate(path, create=True)
    registry = TaskRegistry(path, FrozenClock())
    registry.save(definition())
    store = ContextManifestStore(path, FrozenClock())
    candidate = ModelCandidate(
        "model",
        ModelTier.FREE,
        True,
        True,
        True,
        frozenset(TaskClass),
        frozenset(DataClassification),
        None,
        frozenset(),
    )
    valid = context()
    store.save(valid)
    assert (
        select_for_registered_context(
            "task",
            "v1",
            registry=registry,
            manifest_hash=valid.manifest_hash,
            store=store,
            candidates=(candidate,),
        )
        == candidate
    )
    for changed in (
        context(system="Other system."),
        context(schema={"type": "array"}),
        context(prompt_version="prompt-v2"),
        context(spec_version="spec-v2"),
        context(system_classification=DataClassification.PRIVATE),
        context(schema_classification=DataClassification.PRIVATE),
        context(task_version="v2"),
    ):
        store.save(changed)
        with pytest.raises(RoutingError, match="does not match"):
            select_for_registered_context(
                "task",
                "v1",
                registry=registry,
                manifest_hash=changed.manifest_hash,
                store=store,
                candidates=(candidate,),
            )
    with pytest.raises(TaskDefinitionNotFound):
        select_for_registered_context(
            "task",
            "unregistered",
            registry=registry,
            manifest_hash=valid.manifest_hash,
            store=store,
            candidates=(candidate,),
        )


def test_task_migration_preserves_existing_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bq.db"
    migrations = database.packaged_migrations()
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations[:10])
    assert database.migrate(path, create=True) == 10
    store = ContextManifestStore(path, FrozenClock())
    original = context()
    store.save(original)
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations)
    assert database.migrate(path) == 11
    assert store.load(original.manifest_hash) == original
    database.check_database(path)


def test_registered_context_enforces_section_spec_even_if_manifest_lies(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    database.migrate(path, create=True)
    registry = TaskRegistry(path, FrozenClock())
    section = ContextSection.FACTS
    registry.save(
        replace(
            definition(),
            section_limits=((section, 1),),
            required_sections=frozenset({section}),
        )
    )
    store = ContextManifestStore(path, FrozenClock())
    candidate = ModelCandidate(
        "model",
        ModelTier.FREE,
        True,
        True,
        True,
        frozenset(TaskClass),
        frozenset(DataClassification),
        None,
        frozenset(),
    )
    item = ContextItem(
        section,
        "a",
        "v1",
        NOW - timedelta(seconds=3),
        NOW - timedelta(seconds=2),
        "fact",
        0,
        PUBLIC,
    )
    valid = context(
        items=(item,),
        section_limits={section: 1},
        required_sections=frozenset({section}),
    )
    store.save(valid)
    assert (
        select_for_registered_context(
            "task",
            "v1",
            registry=registry,
            manifest_hash=valid.manifest_hash,
            store=store,
            candidates=(candidate,),
        )
        == candidate
    )

    for source in (context(), context(items=(item, replace(item, source_id="b")))):
        manifest = json.loads(source.manifest_json)
        manifest["section_limits"] = {section.value: 1}
        manifest["required_sections"] = [section.value]
        encoded = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        forged = BuiltContext(
            source.prompt_json, encoded, sha256(encoded.encode("utf-8")).hexdigest()
        )
        store.save(forged)
        with pytest.raises(RoutingError, match="section requirements"):
            select_for_registered_context(
                "task",
                "v1",
                registry=registry,
                manifest_hash=forged.manifest_hash,
                store=store,
                candidates=(candidate,),
            )


def test_task_registry_lists_only_the_latest_version_per_task(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    database.migrate(path, create=True)
    registry = TaskRegistry(path, FrozenClock())
    first = definition()
    second = replace(first, policy=replace(first.policy, version="v2"))
    registry.save(first)
    registry.save(second)
    latest = registry.list_latest()
    assert [item.policy.task_id for item in latest] == ["task"]
    assert [item.policy.version for item in latest] == ["v2"]
    assert latest[0] == second
    with pytest.raises(RoutingError):
        registry.list_latest(limit=0)
