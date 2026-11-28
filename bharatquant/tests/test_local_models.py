import asyncio
from datetime import UTC, datetime

import pytest

from bq.local_models import (
    DEFAULT_OLLAMA_ENDPOINT,
    LocalModelError,
    ModelPullError,
    ModelPullRunner,
    create_local_model,
    installed_ollama_models,
    model_is_installed,
    parse_local_models,
    provider_id,
    seed_from_provider_specs,
    serialize_local_models,
    validate_local_models,
)


def test_ollama_entry_derives_provider_id_and_endpoint() -> None:
    model = create_local_model(
        label="Gemma 4 12B (MLX)",
        kind="ollama",
        model="gemma4:12b-mlx",
    )
    assert model.id == "ollama:chat:gemma4:12b-mlx"
    assert model.id == provider_id("ollama", "gemma4:12b-mlx")
    assert model.endpoint is None
    assert model.resolved_endpoint == DEFAULT_OLLAMA_ENDPOINT
    assert model.num_ctx == 16_384
    assert model.num_predict == 512
    assert model.think is False


def test_openai_compatible_entry_requires_a_loopback_endpoint() -> None:
    model = create_local_model(
        label="Qwen MLX server",
        kind="openai_compatible",
        model="mlx-community/Qwen3.5-9B-MLX-4bit",
        endpoint="http://127.0.0.1:8080/v1",
    )
    assert model.id == "openai:chat:mlx-community/Qwen3.5-9B-MLX-4bit"
    assert model.resolved_endpoint == "http://127.0.0.1:8080/v1"

    with pytest.raises(LocalModelError):
        create_local_model(
            label="Remote",
            kind="openai_compatible",
            model="remote-model",
            endpoint="http://example.com/v1",
        )
    with pytest.raises(LocalModelError):
        create_local_model(
            label="Missing endpoint",
            kind="openai_compatible",
            model="remote-model",
        )


def test_invalid_entries_are_rejected() -> None:
    for kwargs in [
        {"label": "", "kind": "ollama", "model": "m"},
        {"label": "x", "kind": "nope", "model": "m"},
        {"label": "x", "kind": "ollama", "model": ""},
        {"label": "x", "kind": "ollama", "model": "has space"},
        {"label": "x", "kind": "ollama", "model": "m", "num_ctx": 1},
        {"label": "x", "kind": "ollama", "model": "m", "num_predict": 1},
        {"label": "x", "kind": "ollama", "model": "m", "temperature": 3},
        {"label": "x", "kind": "ollama", "model": "m", "think": "yes"},
    ]:
        with pytest.raises(LocalModelError):
            create_local_model(**kwargs)


def test_duplicates_and_the_limit_are_rejected() -> None:
    model = create_local_model(label="a", kind="ollama", model="m")
    with pytest.raises(LocalModelError):
        validate_local_models([model, model])

    models = [
        create_local_model(label=f"m{i}", kind="ollama", model=f"model-{i}")
        for i in range(33)
    ]
    with pytest.raises(LocalModelError):
        validate_local_models(models)


def test_serialise_round_trip_and_malformed_tolerance() -> None:
    created = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)
    model = create_local_model(
        label="Gemma",
        kind="ollama",
        model="gemma4:12b-mlx",
        created_at=created,
    )
    raw = serialize_local_models([model])
    parsed = parse_local_models(raw)
    assert parsed == (model,)

    assert parse_local_models(None) == ()
    assert parse_local_models("not json") == ()
    assert parse_local_models('{"not": "a list"}') == ()
    # One bad entry is skipped, the good one survives.
    assert (
        parse_local_models(
            '[{"label": "", "kind": "ollama", "model": "x"}, ' + raw[1:-1] + "]"
        )
    )[0].model == "gemma4:12b-mlx"


def test_installed_models_are_parsed(monkeypatch) -> None:
    class Completed:
        returncode = 0
        stdout = "NAME  ID  SIZE\nGemma:latest  a  1GB\ngemma4:12b-mlx  b  2GB\n"

    monkeypatch.setattr("bq.local_models.shutil.which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr(
        "bq.local_models.subprocess.run", lambda *args, **kwargs: Completed()
    )
    installed = installed_ollama_models()
    assert installed == {"Gemma:latest", "gemma4:12b-mlx"}

    gemma = create_local_model(label="g", kind="ollama", model="gemma4:12b-mlx")
    assert model_is_installed(gemma, installed) is True
    assert model_is_installed(gemma, {"other"}) is False
    assert model_is_installed(gemma, None) is None


def test_pull_runner_streams_output_and_completes(monkeypatch) -> None:
    lines = [b"pulling manifest\n", b"pulling abc... 50%\n", b"success\n"]

    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not lines:
                raise StopAsyncIteration
            return lines.pop(0)

    class Process:
        pid = 4242
        returncode = 0
        stdout = Stream()

        async def wait(self) -> int:
            return 0

    async def fake_exec(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr("bq.local_models.shutil.which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr("bq.local_models.asyncio.create_subprocess_exec", fake_exec)
    model = create_local_model(label="g", kind="ollama", model="gemma4:12b-mlx")

    async def go():
        runner = ModelPullRunner()
        started = runner.start(model)
        assert started.status == "queued"
        assert runner._task is not None
        await runner._task
        return runner.state(model.id)

    state = asyncio.run(go())
    assert state is not None
    assert state.status == "completed"
    assert "pulling manifest" in state.output


def test_pull_rejects_non_ollama_models() -> None:
    runner = ModelPullRunner()
    model = create_local_model(
        label="MLX",
        kind="openai_compatible",
        model="qwen",
        endpoint="http://127.0.0.1:8080/v1",
    )
    with pytest.raises(ModelPullError):
        runner.start(model)


def test_seed_from_promptfoo_specs() -> None:
    seeded = seed_from_provider_specs(
        [
            ("ollama:chat:gemma4:12b-mlx", "Gemma 4 12B (MLX)"),
            ("ollama:chat:qwen3.5:9b", "Qwen3.5-9B"),
        ]
    )
    assert [model.model for model in seeded] == ["gemma4:12b-mlx", "qwen3.5:9b"]
    assert all(model.kind == "ollama" for model in seeded)
