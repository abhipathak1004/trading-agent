"""Frozen replay: determinism, fail-closed hash check, walk-forward folds."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from bq.research_lab.freeze import FrozenManifestError, freeze_run_inputs
from bq.research_lab.replay import (
    ReplayError,
    WalkForwardSpec,
    bars_from_frame,
    folds_csv,
    replay_from_frozen,
    trades_csv,
)
from bq.strategies import StrategyKind
from bq.strategy_dsl import EntryRule, ExitRule, StrategyDefinition

DAYS = [datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index) for index in range(30)]
# Flat chop, one upward break, then a decline: the SMA cross must fire and exit.
PATTERN = (
    [10000, 10020, 9980] * 4 + [11000] + [10900 - index * 180 for index in range(17)]
)


def _frame() -> pl.DataFrame:
    rows = [
        {
            "symbol": "AAA",
            "ts": day,
            "open": price,
            "high": price + 60,
            "low": price - 60,
            "close": price,
            "volume": 5000,
        }
        for day, price in zip(DAYS, PATTERN, strict=True)
    ]
    return pl.DataFrame(rows, schema_overrides={"ts": pl.Datetime("us", "UTC")})


def _definition() -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id="s1",
        version="1",
        name="probe",
        kind=StrategyKind.RULE,
        timeframe="swing",
        universe=("AAA",),
        entry_rule=EntryRule.SMA_CROSS,
        exit_rule=ExitRule.SMA_CROSS_DOWN,
        lookback=3,
        atr_period=3,
        stop_atr_multiple=6.0,
        target_atr_multiple=12.0,
    )


def _frozen(tmp_path: Path) -> tuple[Path, str]:
    return freeze_run_inputs(_frame(), {"theta": "probe"}, tmp_path)


def test_replay_reproduces_fills_and_metrics(tmp_path: Path) -> None:
    path, digest = _frozen(tmp_path)
    result = replay_from_frozen(
        path,
        digest,
        _definition(),
        start=DAYS[0],
        end=DAYS[-1],
        initial_capital_paise=1_000_000,
    )
    assert result.bars == 30
    assert result.trades, "the fixture must produce fills to be a meaningful test"
    assert result.metrics.charges_paise > 0
    assert result.manifest_sha256 == digest


def test_replay_is_bit_identical_across_calls(tmp_path: Path) -> None:
    path, digest = _frozen(tmp_path)
    arguments = {
        "start": DAYS[0],
        "end": DAYS[-1],
        "initial_capital_paise": 1_000_000,
    }
    first = replay_from_frozen(path, digest, _definition(), **arguments)  # type: ignore[arg-type]
    second = replay_from_frozen(path, digest, _definition(), **arguments)  # type: ignore[arg-type]
    assert first.to_dict() == second.to_dict()


def test_a_tampered_slice_fails_closed(tmp_path: Path) -> None:
    path, digest = _frozen(tmp_path)
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(FrozenManifestError):
        replay_from_frozen(
            path,
            digest,
            _definition(),
            start=DAYS[0],
            end=DAYS[-1],
            initial_capital_paise=1_000_000,
        )


def test_walk_forward_scopes_scoring_to_out_of_sample_dates(tmp_path: Path) -> None:
    path, digest = _frozen(tmp_path)
    result = replay_from_frozen(
        path,
        digest,
        _definition(),
        start=DAYS[0],
        end=DAYS[-1],
        initial_capital_paise=1_000_000,
        walk_forward=WalkForwardSpec(folds=2, holdout_bars=3),
    )
    assert len(result.folds) == 2
    assert result.holdout is not None
    fold_starts = [str(fold["start_utc"]) for fold in result.folds]
    fold_ends = [str(fold["end_utc"]) for fold in result.folds]
    assert fold_starts == sorted(fold_starts)
    assert fold_ends == sorted(fold_ends)
    # Folds must not overlap and the holdout must start after the last fold.
    assert fold_ends[0] < fold_starts[1]
    assert str(result.holdout["start_utc"]) > fold_ends[-1]


def test_impossible_walk_forward_is_refused(tmp_path: Path) -> None:
    path, digest = _frozen(tmp_path)
    with pytest.raises(ReplayError):
        replay_from_frozen(
            path,
            digest,
            _definition(),
            start=DAYS[0],
            end=DAYS[-1],
            initial_capital_paise=1_000_000,
            walk_forward=WalkForwardSpec(folds=1),
        )
    with pytest.raises(ReplayError):
        replay_from_frozen(
            path,
            digest,
            _definition(),
            start=DAYS[0],
            end=DAYS[-1],
            initial_capital_paise=1_000_000,
            walk_forward=WalkForwardSpec(folds=2, holdout_bars=999),
        )


def test_csv_exports_carry_every_row(tmp_path: Path) -> None:
    path, digest = _frozen(tmp_path)
    result = replay_from_frozen(
        path,
        digest,
        _definition(),
        start=DAYS[0],
        end=DAYS[-1],
        initial_capital_paise=1_000_000,
        walk_forward=WalkForwardSpec(folds=2),
    )
    trades = trades_csv(result.trades).strip().splitlines()
    assert trades[0].startswith("symbol,entry_at,exit_at")
    assert len(trades) == len(result.trades) + 1
    folds = folds_csv(result.folds, result.holdout).strip().splitlines()
    assert len(folds) == len(result.folds) + 1
    assert folds[0].startswith("fold,start_utc,end_utc")


def test_bars_round_trip_to_vendor_shape(tmp_path: Path) -> None:
    frame = _frame()
    bars = bars_from_frame(frame)
    assert set(bars) == {"AAA"}
    assert len(bars["AAA"]) == frame.height
    assert bars["AAA"][0].close == 100
