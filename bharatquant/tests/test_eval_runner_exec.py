import asyncio
import os
import shutil
from pathlib import Path

import pytest

from bq.evals.config import resolve_paths
from bq.evals.runner import EvalLaunch, EvalRunner
from bq.evals.store import EvalStore
from bq.infra.database import migrate

FIXTURE = Path(__file__).parent / "fixtures" / "eval_results_sample.json"


class FakeProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.pid = 4242

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    async def wait(self) -> int:
        return self.returncode


def _store(tmp_path: Path) -> EvalStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return EvalStore(path)


def _patch_spawn(monkeypatch, *, returncode: int, write_results: bool):
    store_ref: list[EvalStore] = []

    async def fake_exec(*_args, **_kwargs):
        store = store_ref[0]
        if write_results:
            target = store.artifact_dir(store.active_run().id)  # type: ignore[union-attr]
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURE, target / "results.json")
        return FakeProcess(returncode)

    monkeypatch.setattr("bq.evals.runner.asyncio.create_subprocess_exec", fake_exec)
    return store_ref


def test_failing_cases_still_produce_a_scorecard(tmp_path: Path, monkeypatch) -> None:
    """Promptfoo exits 100 when cases fail; results.json must still be scored."""
    store = _store(tmp_path)
    runner = EvalRunner(store, resolve_paths())
    run = store.create_run(
        models=("Gemma 4 12B (MLX)", "Qwen3.5-9B"),
        categories=("earnings",),
        config_path="c",
    )

    store_ref = _patch_spawn(monkeypatch, returncode=100, write_results=True)
    store_ref.append(store)

    asyncio.run(
        runner._execute(
            EvalLaunch(run=run, providers=("ollama:chat:x",), no_cache=True)
        )
    )

    finished = store.get_run(run.id)
    assert finished.status == "completed"
    assert finished.results_json_path is not None
    assert len(store.get_scores(run.id)) == 2
    assert (store.artifact_dir(run.id) / "scorecard.csv").is_file()


def test_missing_results_file_marks_failed(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    runner = EvalRunner(store, resolve_paths())
    run = store.create_run(models=("a",), categories=(), config_path="c")

    store_ref = _patch_spawn(monkeypatch, returncode=1, write_results=False)
    store_ref.append(store)

    asyncio.run(runner._execute(EvalLaunch(run=run, providers=("x",), no_cache=True)))

    failed = store.get_run(run.id)
    assert failed.status == "failed"
    assert failed.error_message
    assert store.get_scores(run.id) == []


def test_promptfoo_runs_with_the_supported_node_first(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    runner = EvalRunner(store, resolve_paths())
    run = store.create_run(models=("m",), categories=(), config_path="c")
    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return FakeProcess(1)

    monkeypatch.setattr("bq.evals.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "bq.evals.runner.supported_node_dir", lambda: "/opt/homebrew/bin"
    )
    asyncio.run(runner._execute(EvalLaunch(run=run, providers=("x",), no_cache=True)))

    path = captured["env"]["PATH"]  # type: ignore[index]
    assert isinstance(path, str)
    assert path.startswith("/opt/homebrew/bin" + os.pathsep)
    argv = captured["args"]
    assert isinstance(argv, tuple)
    assert argv[argv.index("-c") + 1] == run.config_path


def test_no_supported_node_fails_before_spawn(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    runner = EvalRunner(store, resolve_paths())
    run = store.create_run(models=("m",), categories=(), config_path="c")

    async def fake_exec(*_args, **_kwargs):
        raise AssertionError("must not spawn without a supported Node")

    monkeypatch.setattr("bq.evals.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("bq.evals.runner.supported_node_dir", lambda: None)
    asyncio.run(runner._execute(EvalLaunch(run=run, providers=("x",), no_cache=True)))

    failed = store.get_run(run.id)
    assert failed.status == "failed"
    assert "No supported Node.js" in (failed.error_message or "")


def test_zero_case_results_is_a_failure_not_an_empty_scorecard(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    runner = EvalRunner(store, resolve_paths())
    run = store.create_run(models=("m",), categories=(), config_path="c")

    async def fake_exec(*_args, **_kwargs):
        target = store.artifact_dir(store.active_run().id)  # type: ignore[union-attr]
        target.mkdir(parents=True, exist_ok=True)
        (target / "results.json").write_text(
            '{"results": {"results": []}}', encoding="utf-8"
        )
        return FakeProcess(0)

    monkeypatch.setattr("bq.evals.runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "bq.evals.runner.supported_node_dir", lambda: "/opt/homebrew/bin"
    )
    asyncio.run(runner._execute(EvalLaunch(run=run, providers=("x",), no_cache=True)))

    failed = store.get_run(run.id)
    assert failed.status == "failed"
    assert "no case results" in (failed.error_message or "")
    assert store.get_scores(run.id) == []
    assert not (store.artifact_dir(run.id) / "scorecard.csv").is_file()


@pytest.mark.parametrize("returncode", [0, 100])
def test_both_exit_codes_complete_when_results_exist(
    tmp_path: Path, monkeypatch, returncode: int
) -> None:
    store = _store(tmp_path)
    runner = EvalRunner(store, resolve_paths())
    run = store.create_run(models=("m",), categories=(), config_path="c")
    store_ref = _patch_spawn(monkeypatch, returncode=returncode, write_results=True)
    store_ref.append(store)

    asyncio.run(runner._execute(EvalLaunch(run=run, providers=("x",), no_cache=True)))
    assert store.get_run(run.id).status == "completed"
