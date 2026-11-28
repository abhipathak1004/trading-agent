"""Cross-sectional factor benchmarking over NSE universes.

For each horizon this computes the information coefficient (Pearson), the rank
IC (Spearman), ICIR and a rank-turnover estimate, cross-sectionally per date.
Forward returns are the *label*, so a negative shift is correct here — the
look-ahead prohibition applies to strategy features, not to the target they are
scored against.

Honesty about the universe: there is no point-in-time NSE constituent history in
this repository. When membership is a current snapshot, every result is stamped
``survivorship_bias_warning: true`` rather than presented as if it were
point-in-time. A universe that cannot be resolved is an error, never a network
guess.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import polars as pl

DEFAULT_HORIZONS: Final = (1, 5, 20)
MAX_HORIZONS: Final = 6
MAX_UNIVERSE: Final = 2000
MAX_HORIZON_BARS: Final = 250


class AlphaBenchError(ValueError):
    """The bench cannot be computed from the inputs as described."""


def _finite(value: float) -> float | None:
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _spearman(left: np.ndarray, right: np.ndarray) -> float | None:
    if len(left) < 3:
        return None
    left_rank = left.argsort().argsort().astype(float)
    right_rank = right.argsort().argsort().astype(float)
    left_rank -= left_rank.mean()
    right_rank -= right_rank.mean()
    denominator = math.sqrt(
        float(left_rank @ left_rank) * float(right_rank @ right_rank)
    )
    if denominator == 0:
        return None
    return _finite(float(left_rank @ right_rank) / denominator)


def _pearson(left: np.ndarray, right: np.ndarray) -> float | None:
    if len(left) < 3:
        return None
    left_centred = left - left.mean()
    right_centred = right - right.mean()
    denominator = math.sqrt(
        float(left_centred @ left_centred) * float(right_centred @ right_centred)
    )
    if denominator == 0:
        return None
    return _finite(float(left_centred @ right_centred) / denominator)


@dataclass(frozen=True, slots=True)
class HorizonResult:
    horizon_bars: int
    ic_mean: float | None
    rank_ic_mean: float | None
    icir: float | None
    turnover: float | None
    dates: int
    mean_names: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "horizon_bars": self.horizon_bars,
            "ic_mean": self.ic_mean,
            "rank_ic_mean": self.rank_ic_mean,
            "icir": self.icir,
            "turnover": self.turnover,
            "dates": self.dates,
            "mean_names": self.mean_names,
        }


def _check_bars(bars: pl.DataFrame) -> None:
    required = {"symbol", "ts", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise AlphaBenchError(f"Bars are missing columns: {sorted(missing)}.")
    if bars.height == 0:
        raise AlphaBenchError("Bars are empty.")


def _forward_returns(bars: pl.DataFrame, horizon: int) -> pl.DataFrame:
    return (
        bars.select(["symbol", "ts", "close"])
        .sort(["symbol", "ts"])
        .with_columns(
            (
                pl.col("close").shift(-horizon).over("symbol")
                / pl.col("close").cast(pl.Float64)
                - 1.0
            ).alias("forward_return")
        )
        .filter(pl.col("forward_return").is_not_null())
        .select(["symbol", "ts", "forward_return"])
    )


def _rank_turnover(panel: pl.DataFrame) -> float | None:
    """Mean absolute cross-sectional rank change, normalised to 0..1."""
    changes: list[float] = []
    previous: dict[str, float] = {}
    for _, group in sorted(panel.group_by("ts"), key=lambda item: str(item[0])):
        if group.height < 3:
            continue
        ordered = group.sort("factor_value")
        count = ordered.height
        ranks = {
            str(symbol): float(index)
            for index, symbol in enumerate(ordered["symbol"].to_list())
        }
        if previous:
            shared = [symbol for symbol in ranks if symbol in previous]
            if shared:
                total = sum(abs(ranks[symbol] - previous[symbol]) for symbol in shared)
                changes.append(total / (len(shared) * max(1, count - 1)))
        previous = ranks
    if not changes:
        return None
    return _finite(sum(changes) / len(changes))


def _bench_horizon(
    panel: pl.DataFrame, horizon: int, *, min_names: int
) -> HorizonResult:
    grouped = sorted(
        ((str(key[0]), group) for key, group in panel.group_by("ts")),
        key=lambda item: item[0],
    )
    ics: list[float] = []
    rank_ics: list[float] = []
    name_counts: list[int] = []
    for _, group in grouped:
        clean = group.drop_nulls(["factor_value", "forward_return"])
        if clean.height < min_names:
            continue
        factor = clean["factor_value"].to_numpy().astype(float)
        forward = clean["forward_return"].to_numpy().astype(float)
        ic = _pearson(factor, forward)
        rank_ic = _spearman(factor, forward)
        if ic is None or rank_ic is None:
            continue
        ics.append(ic)
        rank_ics.append(rank_ic)
        name_counts.append(clean.height)
    ic_mean = _finite(float(np.mean(ics))) if ics else None
    rank_ic_mean = _finite(float(np.mean(rank_ics))) if rank_ics else None
    icir: float | None = None
    if len(ics) >= 2:
        dispersion = float(np.std(ics, ddof=1))
        if dispersion > 0 and ic_mean is not None:
            icir = _finite(ic_mean / dispersion)
    return HorizonResult(
        horizon_bars=horizon,
        ic_mean=ic_mean,
        rank_ic_mean=rank_ic_mean,
        icir=icir,
        turnover=_rank_turnover(panel),
        dates=len(ics),
        mean_names=(_finite(float(np.mean(name_counts))) if name_counts else None),
    )


def alpha_bench(
    bars: pl.DataFrame,
    factor: pl.DataFrame,
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    min_names_per_date: int = 5,
    source: str = "local",
    membership_is_current: bool = True,
    missing_symbols: Sequence[str] = (),
    max_universe: int = MAX_UNIVERSE,
) -> dict[str, object]:
    """IC, rank IC, ICIR and turnover per horizon, plus an honest ``_meta``."""
    _check_bars(bars)
    if not isinstance(factor, pl.DataFrame):
        raise AlphaBenchError("The factor must be a polars DataFrame.")
    if not {"symbol", "ts", "factor_value"} <= set(factor.columns):
        raise AlphaBenchError("The factor needs symbol, ts and factor_value.")
    horizons_tuple = tuple(int(item) for item in horizons)
    if not 1 <= len(horizons_tuple) <= MAX_HORIZONS:
        raise AlphaBenchError(f"Give 1 to {MAX_HORIZONS} horizons.")
    if any(item < 1 or item > MAX_HORIZON_BARS for item in horizons_tuple):
        raise AlphaBenchError("A horizon is outside its bound.")
    if len(set(horizons_tuple)) != len(horizons_tuple):
        raise AlphaBenchError("Horizons must be distinct.")
    if min_names_per_date < 3:
        raise AlphaBenchError("At least three names per date are required.")

    universe = sorted({str(item) for item in bars["symbol"].unique().to_list()})
    if len(universe) > max_universe:
        raise AlphaBenchError(f"The universe exceeds {max_universe} symbols.")
    if len(universe) < min_names_per_date:
        raise AlphaBenchError(
            "The universe is smaller than the minimum names per date."
        )

    results: list[HorizonResult] = []
    for horizon in horizons_tuple:
        forward = _forward_returns(bars, horizon)
        panel = factor.join(forward, on=["symbol", "ts"], how="inner")
        if panel.height == 0:
            results.append(HorizonResult(horizon, None, None, None, None, 0, None))
            continue
        results.append(_bench_horizon(panel, horizon, min_names=min_names_per_date))

    return {
        "horizons": [item.to_dict() for item in results],
        "_meta": {
            "source": source,
            "universe": universe,
            "universe_size": len(universe),
            "survivorship_bias_warning": bool(membership_is_current),
            "membership": "current" if membership_is_current else "point_in_time",
            "missing_symbols": list(missing_symbols),
            "horizons": list(horizons_tuple),
            "min_names_per_date": min_names_per_date,
        },
    }


__all__ = [
    "AlphaBenchError",
    "DEFAULT_HORIZONS",
    "HorizonResult",
    "MAX_UNIVERSE",
    "alpha_bench",
]
