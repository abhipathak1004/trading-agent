from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.core import timestamp, utc
from bq.evals.config import resolve_paths
from bq.evals.store import EvalRunActive, EvalRunNotFound, EvalStore
from bq.infra.database import migrate

FIXTURE = Path(__file__).parent / "fixtures" / "eval_results_sample.json"


def _store(tmp_path: Path) -> EvalStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return EvalStore(path)


def test_migration_creates_eval_tables(tmp_path: Path) -> None:
    store = _store(tmp_path)
    import sqlite3

    connection = sqlite3.connect(store.path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'eval%'"
            )
        }
    finally:
        connection.close()
    assert tables == {"eval_runs", "eval_model_scores"}


def test_create_run_lifecycle_and_scores(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.create_run(
        models=("Gemma 4 12B (MLX)", "Qwen3.5-9B"),
        categories=("earnings", "noise_filtering"),
        config_path="evals/promptfooconfig.yaml",
    )
    assert run.status == "queued"
    assert run.models == ("Gemma 4 12B (MLX)", "Qwen3.5-9B")
    assert run.results_json_path is None

    started = store.mark_started(run.id)
    assert started.status == "running"
    assert started.started_at is not None

    summaries = [
        {
            "model": "Qwen3.5-9B",
            "total": 50,
            "overall_pass_rate": 0.9,
            "accuracy_pass_rate": 0.88,
            "json_parse_rate": 1.0,
            "p50_ms": 1200.0,
            "p95_ms": 4100.0,
            "max_ms": 9000.0,
            "brier_score": 0.03,
            "ece_5bin": 0.05,
            "brier_n": 50,
            "cat_rates": {"earnings": 0.9, "noise_filtering": 1.0},
        }
    ]
    scores = store.save_scores(run.id, summaries)
    assert len(scores) == 1
    assert scores[0].model == "Qwen3.5-9B"
    assert scores[0].p95_ms == 4100.0
    assert scores[0].category_rates["earnings"] == 0.9

    completed = store.mark_completed(run.id, str(FIXTURE))
    assert completed.status == "completed"
    assert completed.completed_at is not None
    assert store.get_scores(run.id)[0].brier_score == 0.03


def test_single_run_lock_rejects_a_second_active_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.create_run(models=("a",), categories=(), config_path="c")
    with pytest.raises(EvalRunActive):
        store.create_run(models=("b",), categories=(), config_path="c")
    assert store.active_run() is not None
    assert store.active_run().id == first.id  # type: ignore[union-attr]

    store.mark_cancelled(first.id)
    assert store.active_run() is None
    # A run can start again once the previous one is terminal.
    second = store.create_run(models=("b",), categories=(), config_path="c")
    assert second.status == "queued"


def test_list_runs_is_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.create_run(models=("a",), categories=(), config_path="c")
    store.mark_started(first.id)
    store.mark_completed(first.id, "none")
    second = store.create_run(models=("b",), categories=(), config_path="c")
    runs = store.list_runs()
    assert [run.id for run in runs] == [second.id, first.id]


def test_read_cases_filters_by_status_model_and_category(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.create_run(
        models=("Gemma 4 12B (MLX)",), categories=(), config_path="c"
    )
    store.mark_started(run.id)
    store.mark_completed(run.id, str(FIXTURE))
    scorer = resolve_paths().scorer

    all_cases = store.read_cases(run.id, scorer_path=scorer)
    assert len(all_cases) == 5

    failed = store.read_cases(run.id, scorer_path=scorer, status="failed")
    assert [case.category for case in failed] == ["noise_filtering"]

    passed = store.read_cases(run.id, scorer_path=scorer, status="passed")
    assert len(passed) == 4

    noise = store.read_cases(run.id, scorer_path=scorer, category="noise_filtering")
    assert len(noise) == 2

    qwen = store.read_cases(run.id, scorer_path=scorer, model="Qwen3.5-9B")
    assert len(qwen) == 3


def test_read_cases_requires_results(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.create_run(models=("a",), categories=(), config_path="c")
    with pytest.raises(EvalRunNotFound):
        store.read_cases(run.id, scorer_path=resolve_paths().scorer)


def test_missing_run_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(EvalRunNotFound):
        store.get_run("does-not-exist")


def test_set_config_path_points_at_the_generated_config(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.create_run(models=("a",), categories=(), config_path="placeholder")
    updated = store.set_config_path(run.id, "/artifacts/run/promptfooconfig.yaml")
    assert updated.config_path == "/artifacts/run/promptfooconfig.yaml"
    assert store.get_run(run.id).config_path == "/artifacts/run/promptfooconfig.yaml"
    with pytest.raises(EvalRunNotFound):
        store.set_config_path("missing", "x")


def test_timestamps_are_utc_and_parseable() -> None:
    moment = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    assert timestamp(utc(moment)) == "2026-11-26T12:00:00.000000Z"
