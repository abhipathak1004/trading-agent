import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.cli import main
from bq.context import build_context
from bq.infra.contexts import ContextManifestStore
from bq.infra.database import check_database, migrate
from bq.infra.model_attempts import (
    AttemptConflict,
    AttemptError,
    AttemptNotFound,
    AttemptOutcome,
    AttemptStatus,
    ModelAttempt,
    ModelAttemptStore,
    TokenUsage,
    UsageKind,
)
from bq.model_routing import DataClassification

AS_OF = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self) -> None:
        self.instant = AS_OF + timedelta(hours=1)

    def now(self) -> datetime:
        return self.instant


def fixture(tmp_path: Path) -> tuple[ModelAttemptStore, str, FrozenClock, Path]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    context = build_context(
        (),
        as_of=AS_OF,
        task_id="synthetic-task",
        task_version="v1",
        scope="fixture",
        model_id="synthetic-model",
        prompt_version="p1",
        spec_version="s1",
        tokenizer_id="bytes",
        system="Synthetic prompt",
        system_classification=DataClassification.PUBLIC,
        output_schema={"type": "object"},
        schema_classification=DataClassification.PUBLIC,
        section_limits={},
        required_sections=frozenset(),
        token_limit=1000,
        output_reserve=10,
        overhead_tokens=0,
        count_tokens=lambda text: len(text.encode("utf-8")),
    )
    ContextManifestStore(path, clock).save(context)
    return ModelAttemptStore(path, clock), context.manifest_hash, clock, path


def start(
    store: ModelAttemptStore, manifest_hash: str, attempt_id: str = "attempt-1"
) -> None:
    store.start(
        attempt_id,
        task_id="synthetic-task",
        task_version="v1",
        model_id="synthetic-model",
        manifest_hash=manifest_hash,
    )


def test_attempt_intent_survives_restart_and_unknown_usage_is_not_zero(
    tmp_path: Path,
) -> None:
    store, manifest_hash, clock, path = fixture(tmp_path)
    start(store, manifest_hash)
    reopened = ModelAttemptStore(path, clock)
    assert [attempt.attempt_id for attempt in reopened.incomplete()] == ["attempt-1"]
    assert reopened.get("attempt-1").outcome is None
    with pytest.raises(AttemptConflict, match="already been used"):
        start(reopened, manifest_hash)
    outcome = AttemptOutcome(
        AttemptStatus.TIMEOUT, UsageKind.UNKNOWN, None, None, "timeout"
    )
    finished = reopened.finish("attempt-1", outcome)
    assert finished.outcome == outcome
    assert reopened.incomplete() == ()
    with pytest.raises(AttemptConflict, match="already has an outcome"):
        reopened.finish("attempt-1", outcome)
    with pytest.raises(AttemptNotFound):
        reopened.get("missing")
    check_database(path)


def test_attempt_recording_checks_context_model_and_clock(tmp_path: Path) -> None:
    store, manifest_hash, clock, _ = fixture(tmp_path)
    with pytest.raises(AttemptError, match="does not match"):
        store.start(
            "attempt",
            task_id="task",
            task_version="v1",
            model_id="other",
            manifest_hash=manifest_hash,
        )
    with pytest.raises(AttemptError, match="task does not match"):
        store.start(
            "attempt",
            task_id="synthetic-task",
            task_version="v2",
            model_id="synthetic-model",
            manifest_hash=manifest_hash,
        )
    clock.instant = AS_OF - timedelta(seconds=1)
    with pytest.raises(AttemptError, match="as-of"):
        start(store, manifest_hash)
    clock.instant = AS_OF + timedelta(hours=1)
    start(store, manifest_hash)
    clock.instant -= timedelta(seconds=1)
    with pytest.raises(AttemptError, match="before the start"):
        store.finish(
            "attempt-1",
            AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.REPORTED, 2, 1),
        )
    assert store.get("attempt-1").outcome is None


def test_attempt_usage_requires_explicit_unknown_partial_or_cache_semantics(
    tmp_path: Path,
) -> None:
    store, manifest_hash, _, path = fixture(tmp_path)
    start(store, manifest_hash, "partial")
    partial = AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.REPORTED, 8, None)
    assert store.finish("partial", partial).outcome == partial
    start(store, manifest_hash, "cache")
    cache = AttemptOutcome(AttemptStatus.CACHE_HIT, UsageKind.NO_CALL, 0, 0)
    assert store.finish("cache", cache).outcome == cache
    with pytest.raises(AttemptError, match="Unknown usage"):
        AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.UNKNOWN, 0, 0)
    with pytest.raises(AttemptError, match="zero-token"):
        AttemptOutcome(AttemptStatus.CACHE_HIT, UsageKind.NO_CALL, 0, None)
    with pytest.raises(AttemptError, match="failure code"):
        AttemptOutcome(AttemptStatus.PROVIDER_ERROR, UsageKind.UNKNOWN, None, None)
    with pytest.raises(AttemptError, match="nonnegative"):
        AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.REPORTED, True, 1)
    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM model_attempts")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE model_attempt_outcomes SET input_tokens = 1")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO model_attempt_outcomes VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("bad", AS_OF.isoformat(), "CACHE_HIT", "NO_CALL", 0, None, None),
            )


def test_concurrent_attempt_identity_and_completion_have_one_winner(
    tmp_path: Path,
) -> None:
    store, manifest_hash, _, _ = fixture(tmp_path)

    def create() -> str:
        try:
            start(store, manifest_hash)
            return "created"
        except AttemptConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: create(), range(2))) == [
            "conflict",
            "created",
        ]
    outcome = AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.ESTIMATED, 10, 5)

    def finish() -> str:
        try:
            store.finish("attempt-1", outcome)
            return "finished"
        except AttemptConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: finish(), range(2))) == [
            "conflict",
            "finished",
        ]


def test_incomplete_listing_reads_one_consistent_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, manifest_hash, _, _ = fixture(tmp_path)
    start(store, manifest_hash)
    outcome = AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.REPORTED, 1, 1)
    original_get = ModelAttemptStore._get
    completed = False

    def finish_between_reads(
        connection: sqlite3.Connection, attempt_id: str
    ) -> ModelAttempt:
        nonlocal completed
        if not completed:
            completed = True
            store.finish(attempt_id, outcome)
        return original_get(connection, attempt_id)

    monkeypatch.setattr(ModelAttemptStore, "_get", staticmethod(finish_between_reads))
    snapshot = store.incomplete()
    assert len(snapshot) == 1 and snapshot[0].outcome is None
    assert store.get("attempt-1").outcome == outcome


def test_usage_summary_separates_reported_estimated_missing_and_cache(
    tmp_path: Path,
) -> None:
    store, manifest_hash, _, _ = fixture(tmp_path)
    outcomes = (
        AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.REPORTED, 10, 20),
        AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.REPORTED, 5, None),
        AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.ESTIMATED, 3, 4),
        AttemptOutcome(AttemptStatus.TIMEOUT, UsageKind.UNKNOWN, None, None, "timeout"),
        AttemptOutcome(AttemptStatus.CACHE_HIT, UsageKind.NO_CALL, 0, 0),
    )
    for index, outcome in enumerate(outcomes):
        attempt_id = f"attempt-{index}"
        start(store, manifest_hash, attempt_id)
        store.finish(attempt_id, outcome)
    start(store, manifest_hash, "unfinished")
    (summary,) = store.usage()
    assert (summary.task_id, summary.task_version, summary.model_id) == (
        "synthetic-task",
        "v1",
        "synthetic-model",
    )
    assert (summary.attempt_count, summary.incomplete_count, summary.cache_hits) == (
        6,
        1,
        1,
    )
    assert summary.input == TokenUsage(15, 2, 3, 1, 2)
    assert summary.output == TokenUsage(20, 1, 4, 1, 3)


def test_usage_summary_can_sum_beyond_sqlite_integer_range(tmp_path: Path) -> None:
    store, manifest_hash, _, _ = fixture(tmp_path)
    maximum = 9223372036854775807
    for index in range(2):
        attempt_id = f"maximum-{index}"
        start(store, manifest_hash, attempt_id)
        store.finish(
            attempt_id,
            AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.REPORTED, maximum, 0),
        )
    (summary,) = store.usage()
    assert summary.input.reported_tokens == maximum * 2
    assert summary.input.reported_count == 2


def test_usage_cli_reports_unknown_and_incomplete_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, manifest_hash, _, _ = fixture(tmp_path)
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    assert main(["llm", "usage"]) == 0
    assert json.loads(capsys.readouterr().out) == {"tasks": []}
    start(store, manifest_hash, "unfinished")
    start(store, manifest_hash, "reported")
    store.finish(
        "reported",
        AttemptOutcome(AttemptStatus.SUCCEEDED, UsageKind.REPORTED, 10, None),
    )
    assert main(["llm", "usage"]) == 0
    (summary,) = json.loads(capsys.readouterr().out)["tasks"]
    assert summary["attempt_count"] == 2
    assert summary["incomplete_count"] == 1
    assert summary["input"]["reported_tokens"] == 10
    assert summary["input"]["unknown_count"] == 1
    assert summary["output"]["reported_count"] == 0
    assert summary["output"]["unknown_count"] == 2


def test_usage_api_is_private_uncached_and_preserves_partial_counts(
    tmp_path: Path,
) -> None:
    store, manifest_hash, _, path = fixture(tmp_path)
    auth = OwnerAuth(path)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.get("/api/v1/llm/usage")
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            auth.setup("synthetic owner password")
            response = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": "synthetic owner password"},
            )
            csrf = response.json()["csrf_token"]
            response = await client.get("/api/v1/llm/usage")
            assert response.json() == {
                "total_groups": 0,
                "groups": [],
                "provider_connected": False,
            }
            start(store, manifest_hash, "unfinished")
            start(store, manifest_hash, "reported")
            store.finish(
                "reported",
                AttemptOutcome(
                    AttemptStatus.SUCCEEDED, UsageKind.REPORTED, 9007199254740993, None
                ),
            )
            response = await client.get("/api/v1/llm/usage")
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["total_groups"] == 1
            assert body["provider_connected"] is False
            group = body["groups"][0]
            assert group["incomplete_count"] == 1
            assert group["input"]["reported_tokens"] == "9007199254740993"
            assert group["input"]["unknown_count"] == 1
            assert group["output"]["reported_count"] == 0
            assert group["output"]["unknown_count"] == 2
            assert (
                await client.post(
                    "/api/v1/auth/logout",
                    headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                )
            ).status_code == 204
            assert (await client.get("/api/v1/llm/usage")).status_code == 401

    asyncio.run(check())


def test_usage_api_returns_only_twenty_groups(tmp_path: Path) -> None:
    store, _, clock, path = fixture(tmp_path)
    auth = OwnerAuth(path)
    auth.setup("synthetic owner password")
    contexts = ContextManifestStore(path, clock)
    for index in range(21):
        task_id = f"task-{index:02}"
        context = build_context(
            (),
            as_of=AS_OF,
            task_id=task_id,
            task_version="v1",
            scope="fixture",
            model_id="synthetic-model",
            prompt_version="p1",
            spec_version="s1",
            tokenizer_id="bytes",
            system="Synthetic prompt",
            system_classification=DataClassification.PUBLIC,
            output_schema={"type": "object"},
            schema_classification=DataClassification.PUBLIC,
            section_limits={},
            required_sections=frozenset(),
            token_limit=1000,
            output_reserve=10,
            overhead_tokens=0,
            count_tokens=lambda text: len(text.encode("utf-8")),
        )
        contexts.save(context)
        store.start(
            f"attempt-{index}",
            task_id=task_id,
            task_version="v1",
            model_id="synthetic-model",
            manifest_hash=context.manifest_hash,
        )
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": "synthetic owner password"},
            )
            response = await client.get("/api/v1/llm/usage")
            body = response.json()
            assert body["total_groups"] == 21
            assert len(body["groups"]) == 20
            assert body["groups"][0]["task_id"] == "task-00"
            assert body["groups"][-1]["task_id"] == "task-19"

    asyncio.run(check())
