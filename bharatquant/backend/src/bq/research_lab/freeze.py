"""Frozen run inputs and their two digests (ADR-054).

Upstream Vibe-Trading hashes the Parquet file. That single digest cannot do both
jobs this project needs:

* **Identity.** Two runs must be comparable when their *content* matches, even if
  they were written by different Polars/PyArrow versions. Parquet bytes embed
  writer metadata, so a byte hash changes across a dependency bump and every
  stored run would stop verifying.
* **Tamper detection.** A byte hash is exactly what catches a modified file.

So a freeze records both: ``content_sha256`` over canonical JSON of the sorted
rows (the value stored on ``research_runs.data_manifest_sha256``), and
``file_sha256`` over the bytes (checked against the file beside the run).
Verification requires both to agree, which is also what lets a test show which
layer caught which failure.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import polars as pl

from bq.core import canonical_digest, timestamp
from bq.research_lab.contracts import MarketDataFrameContract

SCHEMA_VERSION: Final = 1
MANIFEST_NAME: Final = "manifest.json"
INPUTS_NAME: Final = "inputs.parquet"


class FreezeError(ValueError):
    """A run's inputs could not be frozen honestly."""


class FrozenManifestError(FreezeError):
    """Frozen inputs no longer match the digest recorded for the run."""


def _row_payload(frame: pl.DataFrame) -> list[dict[str, object]]:
    """The canonical, version-independent form a content digest is taken over."""
    rows: list[dict[str, object]] = []
    for row in frame.sort(["symbol", "ts"]).iter_rows(named=True):
        rows.append(
            {
                "symbol": str(row["symbol"]),
                "ts": timestamp(row["ts"]),
                "open": int(row["open"]),
                "high": int(row["high"]),
                "low": int(row["low"]),
                "close": int(row["close"]),
                "volume": int(row["volume"]),
            }
        )
    return rows


def content_digest(frame: pl.DataFrame) -> str:
    """SHA-256 over canonical JSON of the sorted rows, independent of the writer."""
    return canonical_digest(
        {"schema_version": SCHEMA_VERSION, "rows": _row_payload(frame)}
    )


def _validated_params(params: Mapping[str, object]) -> dict[str, object]:
    payload = dict(params)
    try:
        json.dumps(payload, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FreezeError(
            "Run parameters must be JSON-serialisable and finite."
        ) from exc
    return payload


def freeze_run_inputs(
    frame: pl.DataFrame,
    params: Mapping[str, object],
    run_dir: Path | str,
) -> tuple[Path, str]:
    """Write a deterministic Parquet slice plus its manifest.

    Returns ``(parquet_path, content_sha256)``. The content digest is what a run
    records; the file digest is what verification checks against the bytes.
    """
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    ordered = frame.sort(["symbol", "ts"])
    inputs = directory / INPUTS_NAME
    ordered.write_parquet(str(inputs))
    file_sha = hashlib.sha256(inputs.read_bytes()).hexdigest()
    content_sha = content_digest(ordered)
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "content_sha256": content_sha,
        "file_sha256": file_sha,
        "row_count": ordered.height,
        "columns": sorted(ordered.columns),
        "params": _validated_params(params),
        "polars_version": pl.__version__,
    }
    manifest["manifest_sha256"] = canonical_digest(manifest)
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return inputs, content_sha


def freeze_contract(
    contract: MarketDataFrameContract,
    params: Mapping[str, object],
    run_dir: Path | str,
) -> tuple[Path, str]:
    """Freeze a contract, recording the provenance that makes it interpretable."""
    recorded = dict(params)
    recorded["provenance"] = {
        "source": contract.source,
        "universe": list(contract.universe),
        "currency": contract.currency,
        "price_adjustment": contract.price_adjustment,
        "as_of_utc": timestamp(contract.as_of_utc),
        "bar_interval": contract.bar_interval,
        "missing_symbols": list(contract.missing_symbols),
    }
    return freeze_run_inputs(contract.sorted_frame(), recorded, run_dir)


def read_manifest(parquet_path: Path | str) -> dict[str, object]:
    path = Path(parquet_path)
    manifest_path = path.with_name(MANIFEST_NAME)
    if not manifest_path.is_file():
        raise FrozenManifestError(f"No freeze manifest beside {path.name}.")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise FrozenManifestError("The freeze manifest is not an object.")
    return payload


def verify_frozen_manifest(
    parquet_path: Path | str, expected_content_sha256: str
) -> bool:
    """True only when the content digest, the file digest and the rows all agree.

    Returns ``False`` on every mismatch instead of raising, so a caller can
    report which check failed; :func:`require_frozen_manifest` is the fail-closed
    wrapper used by replay.
    """
    path = Path(parquet_path)
    if not path.is_file():
        return False
    try:
        manifest = read_manifest(path)
        recorded_file = manifest.get("file_sha256")
        recorded_content = manifest.get("content_sha256")
        if not isinstance(recorded_file, str) or not isinstance(recorded_content, str):
            return False
        if expected_content_sha256 != recorded_content:
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != recorded_file:
            return False
        frame = pl.read_parquet(str(path))
        return content_digest(frame) == recorded_content
    except (OSError, ValueError, json.JSONDecodeError, pl.exceptions.PolarsError):
        return False


def require_frozen_manifest(
    parquet_path: Path | str, expected_content_sha256: str
) -> None:
    """Fail closed: replay must not proceed on inputs it cannot verify."""
    if not verify_frozen_manifest(parquet_path, expected_content_sha256):
        raise FrozenManifestError(
            "Frozen inputs do not match the recorded content digest; refusing to "
            "replay."
        )


__all__ = [
    "FrozenManifestError",
    "FreezeError",
    "INPUTS_NAME",
    "MANIFEST_NAME",
    "SCHEMA_VERSION",
    "content_digest",
    "freeze_contract",
    "freeze_run_inputs",
    "read_manifest",
    "require_frozen_manifest",
    "verify_frozen_manifest",
]
