import re
from pathlib import Path

from bq.evals.config import (
    MIN_NODE_VERSION,
    _parse_node_version,
    render_eval_config,
    resolve_paths,
)
from bq.evals.runner import _actionable_error, build_promptfoo_command
from bq.local_models import create_local_model


def test_command_is_always_sequential_and_disable_telemetry_friendly() -> None:
    paths = resolve_paths()
    command = build_promptfoo_command(
        paths=paths,
        artifact_dir=Path("/tmp/run"),
        providers=(),
        no_cache=True,
    )
    assert command[:4] == ["npx", "promptfoo@latest", "eval", "--max-concurrency"]
    assert command[4] == "1"
    assert "--no-cache" in command
    assert "-c" in command and str(paths.config) in command
    assert command[command.index("-o") + 1] == "/tmp/run/results.json"
    # No provider filter means every configured model runs.
    assert "--filter-providers" not in command


def test_provider_subset_uses_one_anchored_regex() -> None:
    ids = ("ollama:chat:qwen3.5:9b", "ollama:chat:gemma4:12b-mlx")
    command = build_promptfoo_command(
        paths=resolve_paths(),
        artifact_dir=Path("/tmp/run"),
        providers=ids,
        no_cache=False,
    )
    index = command.index("--filter-providers")
    pattern = command[index + 1]
    # Promptfoo treats the value as one regex, so a comma list matches nothing.
    assert "," not in pattern
    for model_id in ids:
        assert re.fullmatch(pattern, model_id)
    assert not re.search(pattern, "ollama:chat:qwen3.5:9b-extra")
    assert "--no-cache" not in command


def test_category_filter_is_passed_to_promptfoo() -> None:
    command = build_promptfoo_command(
        paths=resolve_paths(),
        artifact_dir=Path("/tmp/run"),
        providers=(),
        categories=("earnings", "noise_filtering"),
        no_cache=True,
    )
    index = command.index("--filter-metadata")
    assert command[index + 1] == "category=earnings,noise_filtering"


def test_no_categories_means_no_metadata_filter() -> None:
    command = build_promptfoo_command(
        paths=resolve_paths(),
        artifact_dir=Path("/tmp/run"),
        providers=(),
        categories=(),
        no_cache=True,
    )
    assert "--filter-metadata" not in command


def _config_lines() -> list[str]:
    return [
        line.strip()
        for line in resolve_paths().config.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]


def test_config_caps_generation_tokens() -> None:
    """Every Ollama provider must bound output, or a sweep never finishes."""
    lines = _config_lines()
    provider_blocks = sum(1 for line in lines if "ollama:chat:" in line)
    assert provider_blocks >= 1
    assert (
        sum(1 for line in lines if line.startswith("num_predict:")) == provider_blocks
    )


def test_config_disables_thinking_for_strict_json() -> None:
    """Reasoning prose breaks the strict-JSON assertion; think must stay off."""
    lines = _config_lines()
    provider_blocks = sum(1 for line in lines if "ollama:chat:" in line)
    assert sum(1 for line in lines if line == "think: false") == provider_blocks


def test_generated_config_replaces_providers_and_absolutises_the_dataset(
    tmp_path: Path,
) -> None:
    paths = resolve_paths()
    base = paths.config.read_text(encoding="utf-8")
    models = (
        create_local_model(label="Gemma 4 12B", kind="ollama", model="gemma4:12b-mlx"),
        create_local_model(
            label='MLX "server"',
            kind="openai_compatible",
            model="mlx-community/qwen",
            endpoint="http://127.0.0.1:8080/v1",
        ),
    )
    rendered = render_eval_config(base, models, paths.dataset)

    assert "  - id: ollama:chat:gemma4:12b-mlx" in rendered
    assert "  - id: openai:chat:mlx-community/qwen" in rendered
    assert 'apiBaseUrl: "http://127.0.0.1:8080/v1"' in rendered
    assert 'label: "MLX \\"server\\""' in rendered
    assert "num_predict: 512" in rendered
    assert "think: false" in rendered
    # The template's own providers are gone, but the anchor/prompts/tests survive.
    assert "  - id: ollama:chat:granite4.2:8b" not in rendered
    assert "x-trading-tools:" in rendered
    assert "defaultTest:" in rendered
    assert f"tests: file://{paths.dataset.resolve()}" in rendered


def test_command_can_point_at_a_generated_config(tmp_path: Path) -> None:
    generated = tmp_path / "promptfooconfig.yaml"
    command = build_promptfoo_command(
        paths=resolve_paths(),
        artifact_dir=tmp_path,
        providers=(),
        no_cache=False,
        config_path=generated,
    )
    assert command[command.index("-c") + 1] == str(generated)


def test_node_version_parses_and_minimum_matches_promptfoo_engine() -> None:
    # promptfoo@latest requires node >= 22.22.0; a shadowed 22.17 must be skipped.
    assert _parse_node_version("v26.13.1\n") == (26, 13, 1)
    assert _parse_node_version("v22.17.0") == (22, 17, 0)
    assert _parse_node_version("v22.17.0") < MIN_NODE_VERSION
    assert _parse_node_version("garbage") is None
    assert MIN_NODE_VERSION == (22, 22, 0)


def test_actionable_error_prefers_last_meaningful_line() -> None:
    stderr = (
        b"npm warn EBADENGINE Unsupported engine { node >=22.22.0 }\n"
        b"\x1b[33mpromptfoo requires a supported Node.js runtime.\x1b[0m\n"
        b"Detected: v22.17.0\n"
        b"Required: >=22.22.0\n"
    )
    message = _actionable_error(stderr, 1)
    assert message == ">=22.22.0" or "Node" in message
    assert "npm warn" not in message
    assert "\x1b" not in message


def test_actionable_error_falls_back_to_exit_code() -> None:
    assert _actionable_error(None, 7) == "promptfoo exited 7"
    assert _actionable_error(b"   \n  \n", 2) == "promptfoo exited 2"
