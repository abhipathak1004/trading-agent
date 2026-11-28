"""Frozen inputs carry two digests: content identity and byte tamper detection."""

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from bq.research_lab.freeze import (
    FrozenManifestError,
    content_digest,
    freeze_run_inputs,
    read_manifest,
    require_frozen_manifest,
    verify_frozen_manifest,
)


def _frame() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "symbol": "AAA",
                "ts": datetime(2026, 1, 5 + index, tzinfo=UTC),
                "open": price,
                "high": price + 50,
                "low": price - 50,
                "close": price,
                "volume": 1000,
            }
            for index, price in enumerate([10000, 10100, 10050])
        ],
        schema_overrides={"ts": pl.Datetime("us", "UTC")},
    )


def test_manifest_records_both_digests(tmp_path: Path) -> None:
    path, content_sha = freeze_run_inputs(
        _frame(), {"theta": {"product": "MIS"}}, tmp_path
    )
    manifest = read_manifest(path)
    assert manifest["content_sha256"] == content_sha
    assert isinstance(manifest["file_sha256"], str)
    assert len(str(manifest["file_sha256"])) == 64
    assert manifest["row_count"] == 3
    assert verify_frozen_manifest(path, content_sha) is True


def test_content_digest_is_stable_across_a_refreeze(tmp_path: Path) -> None:
    _, first = freeze_run_inputs(_frame(), {"a": 1}, tmp_path / "one")
    _, second = freeze_run_inputs(_frame(), {"a": 1}, tmp_path / "two")
    assert first == second
    assert first == content_digest(_frame())


def test_content_digest_ignores_row_order_in_the_input() -> None:
    assert content_digest(_frame().reverse()) == content_digest(_frame())


def test_one_byte_tamper_fails_the_file_check(tmp_path: Path) -> None:
    path, content_sha = freeze_run_inputs(_frame(), {}, tmp_path)
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    path.write_bytes(bytes(raw))
    # The content identity recorded for the run is unchanged...
    assert content_sha == read_manifest(path)["content_sha256"]
    # ...but the bytes no longer match, so verification fails closed.
    assert verify_frozen_manifest(path, content_sha) is False


def test_a_wrong_expected_digest_is_rejected(tmp_path: Path) -> None:
    path, _ = freeze_run_inputs(_frame(), {}, tmp_path)
    assert verify_frozen_manifest(path, "0" * 64) is False


def test_missing_manifest_is_rejected(tmp_path: Path) -> None:
    path, content_sha = freeze_run_inputs(_frame(), {}, tmp_path)
    (tmp_path / "manifest.json").unlink()
    assert verify_frozen_manifest(path, content_sha) is False


def test_require_frozen_manifest_fails_closed(tmp_path: Path) -> None:
    path, content_sha = freeze_run_inputs(_frame(), {}, tmp_path)
    require_frozen_manifest(path, content_sha)
    with pytest.raises(FrozenManifestError):
        require_frozen_manifest(path, "1" * 64)


def test_non_finite_params_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        freeze_run_inputs(_frame(), {"nan": float("nan")}, tmp_path)
