from pathlib import Path

from bq.evals.config import resolve_paths
from bq.evals.runner import _actionable_error, build_promptfoo_command


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


def test_provider_subset_uses_filter_flag() -> None:
    command = build_promptfoo_command(
        paths=resolve_paths(),
        artifact_dir=Path("/tmp/run"),
        providers=("ollama:chat:qwen3.5:9b", "ollama:chat:gemma4:12b-mlx"),
        no_cache=False,
    )
    index = command.index("--filter-providers")
    assert command[index + 1] == "ollama:chat:qwen3.5:9b,ollama:chat:gemma4:12b-mlx"
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
