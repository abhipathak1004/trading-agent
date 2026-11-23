import json
import sqlite3
from collections.abc import Callable, Mapping
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
from bq.infra.context_routing import select_for_context
from bq.infra.contexts import ContextManifestNotFound, ContextManifestStore
from bq.infra.database import check_database, migrate
from bq.model_routing import (
    DataClassification,
    ModelCandidate,
    ModelTier,
    RouteUnavailable,
    RoutingError,
    TaskClass,
    TaskPolicy,
)

AS_OF = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def item(
    source_id: str,
    *,
    section: ContextSection = ContextSection.FACTS,
    required: bool = False,
    priority: int = 0,
) -> ContextItem:
    return ContextItem(
        section=section,
        source_id=source_id,
        version_id="v1",
        known_at=AS_OF - timedelta(hours=2),
        recorded_at=AS_OF - timedelta(hours=1),
        content=f"content for {source_id}",
        priority=priority,
        classification=DataClassification.PUBLIC,
        required=required,
    )


def count_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def build(
    items: tuple[ContextItem, ...],
    *,
    as_of: datetime = AS_OF,
    output_schema: Mapping[str, object] | None = None,
    section_limits: Mapping[ContextSection, int] | None = None,
    required_sections: frozenset[ContextSection] = frozenset(),
    token_limit: int = 10000,
    count_tokens: Callable[[str], int] = count_bytes,
    system_classification: DataClassification = DataClassification.PUBLIC,
    schema_classification: DataClassification = DataClassification.PUBLIC,
) -> BuiltContext:
    return build_context(
        items,
        as_of=as_of,
        task_id="synthetic-task",
        task_version="v1",
        scope="fixture",
        model_id="test-model",
        prompt_version="prompt-v1",
        spec_version="spec-v1",
        tokenizer_id="utf8-bytes",
        system="Use the supplied evidence.",
        system_classification=system_classification,
        output_schema=output_schema
        if output_schema is not None
        else {"type": "object"},
        schema_classification=schema_classification,
        section_limits=section_limits if section_limits is not None else {},
        required_sections=required_sections,
        token_limit=token_limit,
        output_reserve=10,
        overhead_tokens=2,
        count_tokens=count_tokens,
    )


def test_context_is_deterministic_and_excludes_late_or_duplicate_versions() -> None:
    first = item("a", priority=2)
    second = item("b", section=ContextSection.NOTES)
    late = replace(item("late"), recorded_at=AS_OF + timedelta(seconds=1))
    result = build((second, late, first, first))
    assert result == build((first, second, first, late))
    manifest = json.loads(result.manifest_json)
    assert [entry["source_id"] for entry in manifest["included"]] == ["a", "b"]
    assert [(entry["source_id"], entry["reason"]) for entry in manifest["dropped"]] == [
        ("a", "duplicate"),
        ("late", "after_as_of"),
    ]
    assert (
        manifest["included"][0]["content_hash"]
        == sha256(first.content.encode()).hexdigest()
    )
    assert result.manifest_hash == sha256(result.manifest_json.encode()).hexdigest()
    assert result.prompt_json == manifest["prompt_json"]
    assert manifest["prompt_hash"] == sha256(result.prompt_json.encode()).hexdigest()
    prompt = json.loads(result.prompt_json)
    assert list(prompt) == ["system", "sections", "output_schema"]
    assert list(prompt["sections"]) == [
        "reference",
        "facts",
        "notes",
        "documents",
        "memory",
        "task_input",
    ]


def test_context_enforces_limits_and_never_drops_required_content() -> None:
    high = item("high", priority=10)
    low = item("low", priority=1)
    required = item("required", required=True)
    result = build(
        (low, high, required),
        section_limits={ContextSection.FACTS: 2},
        required_sections=frozenset({ContextSection.FACTS}),
    )
    manifest = json.loads(result.manifest_json)
    assert [entry["source_id"] for entry in manifest["included"]] == [
        "high",
        "required",
    ]
    assert manifest["dropped"][0]["reason"] == "section_limit"
    with pytest.raises(ContextError, match="section limit"):
        build((required,), section_limits={ContextSection.FACTS: 0})
    with pytest.raises(ContextError, match="section is unavailable"):
        build((), required_sections=frozenset({ContextSection.FACTS}))
    with pytest.raises(ContextError, match="unavailable at the requested time"):
        build((replace(required, recorded_at=AS_OF + timedelta(seconds=1)),))


def test_context_manifest_classifies_every_rendered_input() -> None:
    public = item("public")
    private = replace(
        item("private"), classification=DataClassification.PRIVATE, priority=1
    )
    manifest = json.loads(build((public, private)).manifest_json)
    assert manifest["data_classes"] == ["PRIVATE", "PUBLIC"]
    assert {
        entry["source_id"]: entry["classification"] for entry in manifest["included"]
    } == {"private": "PRIVATE", "public": "PUBLIC"}
    dropped = json.loads(
        build((public, private), section_limits={ContextSection.FACTS: 1}).manifest_json
    )
    assert dropped["data_classes"] == ["PRIVATE", "PUBLIC"]
    assert dropped["dropped"][0]["classification"] == "PUBLIC"
    dropped_private = json.loads(
        build(
            (replace(public, priority=2), private),
            section_limits={ContextSection.FACTS: 1},
        ).manifest_json
    )
    assert dropped_private["data_classes"] == ["PUBLIC"]
    assert dropped_private["dropped"][0]["classification"] == "PRIVATE"
    system_private = json.loads(
        build((), system_classification=DataClassification.PRIVATE).manifest_json
    )
    assert system_private["data_classes"] == ["PRIVATE", "PUBLIC"]
    schema_private = json.loads(
        build((), schema_classification=DataClassification.PRIVATE).manifest_json
    )
    assert schema_private["data_classes"] == ["PRIVATE", "PUBLIC"]
    with pytest.raises(ContextError, match="classification"):
        replace(public, classification="PUBLIC")  # type: ignore[arg-type]
    with pytest.raises(ContextError, match="classifications"):
        build((), system_classification="PUBLIC")  # type: ignore[arg-type]


def test_context_budget_counts_the_full_prompt_and_drops_only_optional_items() -> None:
    required = item("required", required=True)
    optional = item("optional", priority=1)
    required_only = build((required,))
    exact_limit = len(required_only.prompt_json.encode()) + 12
    trimmed = build((required, optional), token_limit=exact_limit)
    manifest = json.loads(trimmed.manifest_json)
    assert trimmed.prompt_json == required_only.prompt_json
    assert (
        manifest["input_tokens"]
        + manifest["output_reserve"]
        + manifest["overhead_tokens"]
        == exact_limit
    )
    assert [(entry["source_id"], entry["reason"]) for entry in manifest["dropped"]] == [
        ("optional", "token_budget")
    ]
    with pytest.raises(ContextError, match="token budget"):
        build((required,), token_limit=exact_limit - 1)
    with pytest.raises(ContextError, match="token budget"):
        build(
            (optional,),
            required_sections=frozenset({ContextSection.FACTS}),
            token_limit=1,
        )


def test_context_rejects_conflicting_versions_and_invalid_inputs() -> None:
    original = item("same")
    with pytest.raises(ContextError, match="Conflicting"):
        build((original, replace(original, content="different")))
    with pytest.raises(ContextError, match="timezone-aware"):
        build((original,), as_of=AS_OF.replace(tzinfo=None))
    with pytest.raises(ContextError, match="nonnegative"):
        build((original,), count_tokens=lambda _: -1)
    with pytest.raises(ContextError, match="finite JSON"):
        build((original,), output_schema={"x": float("nan")})
    with pytest.raises(ContextError, match="before it is known"):
        replace(original, recorded_at=original.known_at - timedelta(seconds=1))
    with pytest.raises(ContextError, match="UTF-8"):
        build((replace(original, content="\ud800"),))
    with pytest.raises(ContextError, match="item limit"):
        build((original,) * 257)
    with pytest.raises(ContextError, match="byte limit"):
        build((replace(original, content="x" * (1024 * 1024)),))


class FrozenClock:
    def now(self) -> datetime:
        return AS_OF + timedelta(hours=1)


def test_context_manifest_persists_idempotently_and_is_append_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ContextManifestStore(path, FrozenClock())
    context = build((item("fixture"),))
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(store.save, (context, context)))
    assert store.load(context.manifest_hash) == context
    with pytest.raises(ContextManifestNotFound):
        store.load("0" * 64)
    with pytest.raises(ContextError, match="integrity"):
        store.save(replace(context, prompt_json="tampered"))
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM context_manifests"
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM context_manifests")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE context_manifests SET manifest_json = '{}'")
    check_database(path)


def test_context_manifest_detects_corruption_and_rejects_future_as_of(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ContextManifestStore(path, FrozenClock())
    context = build((item("fixture"),))
    future = build((item("fixture"),), as_of=AS_OF + timedelta(hours=2))
    with pytest.raises(ContextError, match="future"):
        store.save(future)
    store.save(context)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER context_manifests_no_update")
        connection.execute("UPDATE context_manifests SET manifest_json = '{}'")
    with pytest.raises(sqlite3.DatabaseError, match="integrity"):
        store.load(context.manifest_hash)


def test_context_routing_uses_only_rendered_classification_and_bound_model(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ContextManifestStore(path, FrozenClock())
    task = TaskPolicy(
        "synthetic-task",
        "v1",
        TaskClass.REASONING,
        frozenset(DataClassification),
        frozenset(ModelTier),
    )
    free = ModelCandidate(
        "test-model",
        ModelTier.FREE,
        True,
        True,
        True,
        frozenset(TaskClass),
        frozenset(DataClassification),
        None,
        frozenset(),
    )
    paid = replace(free, model_id="other-model", tier=ModelTier.PAID)
    public = replace(item("public"), priority=2)
    private = replace(item("private"), classification=DataClassification.PRIVATE)
    private_context = build((public, private))
    store.save(private_context)
    with pytest.raises(RouteUnavailable, match="private_data_disallowed_on_free_tier"):
        select_for_context(
            task,
            manifest_hash=private_context.manifest_hash,
            store=store,
            candidates=(free, paid),
        )
    matching_paid = replace(free, tier=ModelTier.PAID)
    assert (
        select_for_context(
            task,
            manifest_hash=private_context.manifest_hash,
            store=store,
            candidates=(matching_paid,),
        )
        == matching_paid
    )
    for context in (
        build((), system_classification=DataClassification.PRIVATE),
        build((), schema_classification=DataClassification.PRIVATE),
    ):
        store.save(context)
        with pytest.raises(
            RouteUnavailable, match="private_data_disallowed_on_free_tier"
        ):
            select_for_context(
                task,
                manifest_hash=context.manifest_hash,
                store=store,
                candidates=(free,),
            )
    with pytest.raises(RouteUnavailable, match="rebuild"):
        select_for_context(
            task,
            manifest_hash=private_context.manifest_hash,
            store=store,
            candidates=(paid,),
        )
    public_context = build((public, private), section_limits={ContextSection.FACTS: 1})
    store.save(public_context)
    assert (
        select_for_context(
            task,
            manifest_hash=public_context.manifest_hash,
            store=store,
            candidates=(free, paid),
        )
        == free
    )
    with pytest.raises(RoutingError, match="does not match"):
        select_for_context(
            replace(task, version="v2"),
            manifest_hash=public_context.manifest_hash,
            store=store,
            candidates=(free,),
        )
    with pytest.raises(RoutingError, match="unique"):
        select_for_context(
            task,
            manifest_hash=public_context.manifest_hash,
            store=store,
            candidates=(free, free),
        )


@pytest.mark.parametrize("change", ["legacy", "classes", "provenance"])
def test_context_routing_rejects_incomplete_or_inconsistent_metadata(
    tmp_path: Path,
    change: str,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ContextManifestStore(path, FrozenClock())
    original = build((item("fixture"),))
    manifest = json.loads(original.manifest_json)
    if change == "legacy":
        del manifest["system_classification"]
    elif change == "classes":
        manifest["data_classes"] = ["PRIVATE"]
    else:
        manifest["included"][0]["content_hash"] = "0" * 64
    manifest_json = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    context = BuiltContext(
        original.prompt_json, manifest_json, sha256(manifest_json.encode()).hexdigest()
    )
    store.save(context)
    task = TaskPolicy(
        "synthetic-task",
        "v1",
        TaskClass.SMALL,
        frozenset(DataClassification),
        frozenset(ModelTier),
    )
    with pytest.raises(RoutingError):
        select_for_context(
            task, manifest_hash=context.manifest_hash, store=store, candidates=()
        )
