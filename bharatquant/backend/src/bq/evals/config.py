"""Deterministic, dependency-free reading of the Promptfoo eval config.

The repo does not depend on PyYAML. The config we ship is small and versioned by
us, so a narrow extractor is enough: strip comment-only lines, isolate the
top-level ``providers:`` block, and read each ``- id:`` with its ``label:``.
Commented-out provider templates (the ``openai:chat:*`` mlx_lm option) are
ignored because their lines begin with ``#``.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from bq.local_models import LocalModel

DEFAULT_EVAL_DIRNAME = "evals"
DEFAULT_CONFIG_NAME = "promptfooconfig.yaml"
DEFAULT_DATASET_NAME = "trading_golden_eval_50.csv"
CATEGORY_COLUMN = "__metadata:category"
ENV_EVALS_DIR = "BQ_EVALS_DIR"
ENV_CONFIG_DIR = "BQ_CONFIG_DIR"

MIN_NODE_VERSION = (22, 22, 0)
_NODE_VERSION_PATTERN = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_NODE_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


class EvalConfigError(ValueError):
    """The eval configuration on disk is missing or malformed."""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str
    label: str

    @property
    def model(self) -> str:
        """The bare model name after the ``provider:kind:`` prefix."""
        parts = self.id.split(":", 2)
        return parts[2] if len(parts) == 3 else self.id


@dataclass(frozen=True, slots=True)
class EvalsPaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / DEFAULT_CONFIG_NAME

    @property
    def dataset(self) -> Path:
        return self.root / DEFAULT_DATASET_NAME

    @property
    def scorer(self) -> Path:
        return self.root / "score_promptfoo_results.py"

    @property
    def agent_settings_mirror(self) -> Path:
        """Readable projection of the SQLite routing; ``BQ_CONFIG_DIR`` overrides."""
        override = os.environ.get(ENV_CONFIG_DIR)
        base = Path(override).expanduser() if override else self.root.parent / "config"
        return base / "agent_settings.yaml"


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from this file until a directory containing the eval bundle is found."""
    anchor = (start or Path(__file__)).resolve()
    for candidate in [anchor, *anchor.parents]:
        if (candidate / DEFAULT_EVAL_DIRNAME / DEFAULT_CONFIG_NAME).is_file():
            return candidate
    raise EvalConfigError(
        f"Could not locate {DEFAULT_EVAL_DIRNAME}/{DEFAULT_CONFIG_NAME} above {anchor}."
    )


def resolve_paths(*, repo_root: Path | None = None) -> EvalsPaths:
    """Resolve the eval directory, honouring ``BQ_EVALS_DIR`` when set."""
    override = os.environ.get(ENV_EVALS_DIR)
    if override:
        return EvalsPaths(root=Path(override).expanduser().resolve())
    if repo_root is not None:
        return EvalsPaths(root=(repo_root / DEFAULT_EVAL_DIRNAME).resolve())
    return EvalsPaths(root=find_repo_root() / DEFAULT_EVAL_DIRNAME)


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def parse_promptfoo_config(text: str) -> list[ProviderSpec]:
    """Return the active providers declared under the top-level ``providers:``."""
    providers: list[ProviderSpec] = []
    current_id: str | None = None
    current_label: str | None = None
    in_block = False

    def flush() -> None:
        nonlocal current_id, current_label
        if current_id:
            providers.append(
                ProviderSpec(id=current_id, label=current_label or current_id)
            )
        current_id = None
        current_label = None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indented = raw[0] in " \t"
        if not indented and stripped.rstrip(":") == "providers":
            in_block = True
            continue
        if not indented:
            if in_block:
                flush()
            in_block = False
            continue
        if not in_block:
            continue
        if stripped.startswith("- "):
            flush()
            entry = stripped[2:].strip()
            if entry.startswith("id:"):
                current_id = _unquote(entry[len("id:") :])
        elif stripped.startswith("label:") and current_id is not None:
            current_label = _unquote(stripped[len("label:") :])

    flush()
    if not providers:
        raise EvalConfigError("No active providers found in the Promptfoo config.")
    return providers


def _provider_lines(model: LocalModel) -> list[str]:
    lines = [
        f"  - id: {model.id}",
        f"    label: {json.dumps(model.label)}",
        "    config:",
        f"      temperature: {model.temperature!r}",
    ]
    if model.kind == "ollama":
        lines.extend(
            [
                f"      num_ctx: {model.num_ctx}",
                f"      num_predict: {model.num_predict}",
                f"      think: {'true' if model.think else 'false'}",
            ]
        )
    else:
        lines.extend(
            [
                f"      apiBaseUrl: {json.dumps(model.resolved_endpoint)}",
                "      apiKeyRequired: false",
                '      apiKey: "not-needed"',
                f"      max_tokens: {model.num_predict}",
            ]
        )
    lines.append("      tools: *trading_tools")
    return lines


def render_eval_config(
    base_text: str, models: Sequence[LocalModel], dataset: Path
) -> str:
    """Replace the providers block and make the dataset path absolute.

    The generated config lives in the run artifact directory, so Promptfoo's
    relative ``tests: file://…`` would resolve against the wrong directory.
    """
    if not models:
        raise EvalConfigError(
            "At least one model is required to render an eval config."
        )
    lines = base_text.splitlines()
    start = None
    for index, raw in enumerate(lines):
        if raw and raw[0] not in " \t" and raw.strip().rstrip(":") == "providers":
            start = index
            break
    if start is None:
        raise EvalConfigError("The Promptfoo config has no top-level providers block.")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        raw = lines[index]
        stripped = raw.strip()
        if raw and raw[0] not in " \t" and stripped and not stripped.startswith("#"):
            end = index
            break

    block = ["providers:"]
    for model in models:
        block.extend(_provider_lines(model))
        block.append("")
    rendered = "\n".join([*lines[:start], *block, *lines[end:]])
    absolute = dataset.resolve()
    rendered = re.sub(
        r"(?m)^tests:\s*file://(?!\/).*$",
        f"tests: file://{absolute}",
        rendered,
    )
    return rendered if rendered.endswith("\n") else rendered + "\n"


def write_run_config(
    base_text: str, models: Sequence[LocalModel], dataset: Path, destination: Path
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_eval_config(base_text, models, dataset), encoding="utf-8"
    )
    return destination


def read_categories(path: Path) -> list[str]:
    """Return the sorted, de-duplicated ``__metadata:category`` values."""
    if not path.is_file():
        raise EvalConfigError(f"Golden dataset is missing: {path.name}.")
    categories: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or CATEGORY_COLUMN not in reader.fieldnames:
            raise EvalConfigError(f"Golden dataset has no {CATEGORY_COLUMN!r} column.")
        for row in reader:
            value = (row.get(CATEGORY_COLUMN) or "").strip()
            if value:
                categories.add(value)
    if not categories:
        raise EvalConfigError("Golden dataset declares no categories.")
    return sorted(categories)


def _parse_node_version(raw: str) -> tuple[int, int, int] | None:
    match = _NODE_VERSION_PATTERN.fullmatch(raw.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _node_search_dirs() -> list[str]:
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    dirs.extend(_NODE_FALLBACK_DIRS)
    ordered: list[str] = []
    for directory in dirs:
        if directory and directory not in ordered:
            ordered.append(directory)
    return ordered


@lru_cache(maxsize=1)
def supported_node_dir() -> str | None:
    """First PATH directory whose ``node`` satisfies Promptfoo's engine range."""
    for directory in _node_search_dirs():
        node = Path(directory) / "node"
        if not node.is_file():
            continue
        try:
            completed = subprocess.run(
                [str(node), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        version = _parse_node_version(completed.stdout)
        if version is not None and version >= MIN_NODE_VERSION:
            return directory
    return None


def tool_availability() -> dict[str, bool]:
    """Report whether the local tools an eval needs are usable."""
    node_dir = supported_node_dir()
    return {
        "npx": node_dir is not None and shutil.which("npx", path=node_dir) is not None,
        "ollama": shutil.which("ollama") is not None,
    }
