"""aihf's model registry, read from the sidecar.

``aihf`` derives the provider from the model id, so the id a user picks has to
be one aihf knows and one whose key is configured. Its registry also drifts
from what vendors actually serve, so the UI treats ``runnable`` as
"aihf knows this id *and* this project holds its key" — nothing stronger.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from bq.aihf.config import AiHfPaths

PROBE_DIR = Path(__file__).resolve().parent / "probe"
REGISTRY_PROBE = PROBE_DIR / "registry.py"
PROBE_TIMEOUT_SECONDS = 30.0


class AiHfRegistryError(RuntimeError):
    """The sidecar is missing or the probe could not be run."""


@dataclass(frozen=True, slots=True)
class AiHfModel:
    display_name: str
    model_name: str
    provider: str


def _run_probe(
    paths: AiHfPaths, script: Path, payload: bytes | None = None
) -> dict[str, object]:
    if not paths.available:
        raise AiHfRegistryError(
            f"The aihf sidecar is not installed at {paths.venv}; run `just aihf-install`."
        )
    try:
        completed = subprocess.run(
            [str(paths.python), str(script)],
            input=payload if payload is not None else b"",
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AiHfRegistryError("The aihf probe did not finish in time.") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise AiHfRegistryError(f"Could not run the aihf probe: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[-400:]
        raise AiHfRegistryError(
            f"The aihf probe failed: {detail or completed.returncode}"
        )
    try:
        parsed = json.loads(completed.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise AiHfRegistryError("The aihf probe returned unreadable output.") from exc
    if not isinstance(parsed, dict):
        raise AiHfRegistryError("The aihf probe returned an unexpected payload.")
    return parsed


def run_probe(
    paths: AiHfPaths, script: Path, *, payload: bytes = b""
) -> dict[str, object]:
    """Run one sidecar script, returning its JSON object.

    Used by the mandate validator and the ledger verifier. Raises
    ``AiHfRegistryError`` when the sidecar is absent or unreadable, so callers
    can distinguish "cannot ask" from "the answer is no".
    """
    return _run_probe(paths, script, payload)


@lru_cache(maxsize=4)
def _registry(venv: str, stamp: float) -> tuple[AiHfModel, ...]:
    """Cached per sidecar install; ``stamp`` busts the cache on reinstall."""
    paths = AiHfPaths(root=Path(venv).parent / "aihf", venv=Path(venv))
    del stamp
    payload = _run_probe(paths, REGISTRY_PROBE)
    entries = payload.get("models")
    if not isinstance(entries, list):
        raise AiHfRegistryError("The aihf registry is missing its model list.")
    models: list[AiHfModel] = []
    for entry in entries:
        if not isinstance(entry, list) or len(entry) != 3:
            continue
        display, model_id, provider = (str(part) for part in entry)
        if model_id:
            models.append(AiHfModel(display, model_id, provider))
    return tuple(models)


def registry(paths: AiHfPaths) -> tuple[AiHfModel, ...]:
    """Every model aihf's picker offers, in its file order."""
    stamp = paths.python.stat().st_mtime if paths.python.is_file() else 0.0
    return _registry(str(paths.venv), stamp)


def provider_for(model_id: str, paths: AiHfPaths) -> str | None:
    """The provider serving *model_id*, or ``None`` if aihf would guess."""
    return next(
        (model.provider for model in registry(paths) if model.model_name == model_id),
        None,
    )
