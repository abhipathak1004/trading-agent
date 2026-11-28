"""Deterministic, dependency-free reading of the Promptfoo eval config.

The repo does not depend on PyYAML. The config we ship is small and versioned by
us, so a narrow extractor is enough: strip comment-only lines, isolate the
top-level ``providers:`` block, and read each ``- id:`` with its ``label:``.
Commented-out provider templates (the ``openai:chat:*`` mlx_lm option) are
ignored because their lines begin with ``#``.
"""

from __future__ import annotations

import csv
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EVAL_DIRNAME = "evals"
DEFAULT_CONFIG_NAME = "promptfooconfig.yaml"
DEFAULT_DATASET_NAME = "trading_golden_eval_50.csv"
CATEGORY_COLUMN = "__metadata:category"
ENV_EVALS_DIR = "BQ_EVALS_DIR"
ENV_CONFIG_DIR = "BQ_CONFIG_DIR"


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


def tool_availability() -> dict[str, bool]:
    """Report whether the local tools an eval needs are on ``PATH``."""
    return {
        "npx": shutil.which("npx") is not None,
        "ollama": shutil.which("ollama") is not None,
    }
