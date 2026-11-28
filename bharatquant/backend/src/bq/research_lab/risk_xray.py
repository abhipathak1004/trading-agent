"""Risk X-Ray: observed drawdown episodes and signed risk contributions.

Two questions a single "max drawdown" number cannot answer:

* *When* did the portfolio draw down, how deep, for how long, and did it recover?
  An open drawdown is materially different from a recovered one, so it is
  reported even when it is not among the deepest.
* *Which holding* carried the risk? Component volatility adds up to total
  volatility, so a hedge shows a negative share and one name can exceed 100%.

Nothing here is a forecast or a return attribution. It is a historical diagnosis
of a constant-weight basket: ``contribution_i = w_i · Cov(r_i, r_p) / σ_p``,
annualised with the same sample convention as total volatility.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Final

import numpy as np

DEFAULT_PERIODS_PER_YEAR: Final = 252
# A value within this relative tolerance of the running peak counts as *at* the
# peak, so floating-point dust cannot add a phantom recovery bar.
DEFAULT_RECOVERY_RTOL: Final = 1e-12
_MAX_EPISODES: Final = 5


class RiskXRayError(ValueError):
    """The inputs cannot produce an honest risk description."""


def _as_float(value: object) -> float:
    """Sort key helper: a missing contribution sorts as zero magnitude."""
    return float(value) if isinstance(value, (int, float)) else 0.0


def _episode(
    *,
    peak_index: int,
    peak_value: float,
    trough_index: int,
    trough_value: float,
    recovery_index: int | None,
    last_index: int,
    recovery_rtol: float,
) -> dict[str, object]:
    depth_pct = (trough_value - peak_value) / peak_value * 100.0
    end_index = last_index if recovery_index is None else recovery_index
    return {
        "peak_index": peak_index,
        "trough_index": trough_index,
        "recovery_index": recovery_index,
        "depth_pct": depth_pct,
        "duration_bars": end_index - peak_index,
        "is_open": recovery_index is None,
        "recovery_rtol": recovery_rtol,
    }


def drawdown_episodes(
    equity: Sequence[float],
    *,
    recovery_rtol: float = DEFAULT_RECOVERY_RTOL,
    top: int = _MAX_EPISODES,
) -> dict[str, object]:
    """The ``top`` deepest observed drawdowns, plus any open one."""
    if len(equity) < 2:
        raise RiskXRayError("At least two equity observations are required.")
    values = [float(item) for item in equity]
    if any(not math.isfinite(item) for item in values):
        raise RiskXRayError("Equity observations must be finite.")
    if any(item <= 0 for item in values):
        raise RiskXRayError("Equity observations must be positive.")
    if top < 1:
        raise RiskXRayError("At least one episode must be requested.")

    episodes: list[dict[str, object]] = []
    peak_index = 0
    peak_value = values[0]
    open_peak: int | None = None
    open_trough: int | None = None
    open_trough_value = math.inf

    for index in range(1, len(values)):
        value = values[index]
        at_or_above_peak = value >= peak_value - abs(peak_value) * recovery_rtol
        if at_or_above_peak:
            if open_peak is not None and open_trough is not None:
                episodes.append(
                    _episode(
                        peak_index=open_peak,
                        peak_value=values[open_peak],
                        trough_index=open_trough,
                        trough_value=open_trough_value,
                        recovery_index=index,
                        last_index=len(values) - 1,
                        recovery_rtol=recovery_rtol,
                    )
                )
                open_peak = None
                open_trough = None
                open_trough_value = math.inf
            if value > peak_value:
                peak_value = value
                peak_index = index
            continue
        if open_peak is None:
            open_peak = peak_index
            open_trough = index
            open_trough_value = value
        elif value < open_trough_value:
            open_trough = index
            open_trough_value = value

    if open_peak is not None and open_trough is not None:
        episodes.append(
            _episode(
                peak_index=open_peak,
                peak_value=values[open_peak],
                trough_index=open_trough,
                trough_value=open_trough_value,
                recovery_index=None,
                last_index=len(values) - 1,
                recovery_rtol=recovery_rtol,
            )
        )

    deepest = sorted(episodes, key=lambda item: float(str(item["depth_pct"])))[:top]
    selected = list(deepest)
    if not any(bool(item["is_open"]) for item in selected):
        open_items = [item for item in episodes if bool(item["is_open"])]
        selected.extend(open_items)
    return {
        "episodes": selected,
        "deepest_count": len(deepest),
        "observed_count": len(episodes),
        "bars": len(values),
        "recovery_rtol": recovery_rtol,
    }


def risk_contributions(
    returns: Mapping[str, Sequence[float]],
    weights: Mapping[str, float],
    *,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
) -> dict[str, object]:
    """Signed annualised volatility contribution per holding."""
    if periods_per_year <= 0:
        raise RiskXRayError("Periods per year must be positive.")
    symbols = [
        symbol for symbol in weights if symbol in returns and len(returns[symbol]) > 0
    ]
    if not symbols:
        raise RiskXRayError("No weighted asset has returns.")
    for symbol in symbols:
        if weights[symbol] < 0:
            raise RiskXRayError(
                "Weights must not be negative; short a hedge leg instead."
            )
    length = min(len(returns[symbol]) for symbol in symbols)
    if length < 2:
        raise RiskXRayError("At least two return observations are required.")

    matrix = np.array(
        [
            [float(returns[symbol][index]) for index in range(length)]
            for symbol in symbols
        ],
        dtype=float,
    )
    if not np.isfinite(matrix).all():
        raise RiskXRayError("Returns must be finite.")
    weight_vector = np.array(
        [float(weights[symbol]) for symbol in symbols], dtype=float
    )
    portfolio = weight_vector @ matrix
    portfolio_std = float(np.std(portfolio, ddof=1))
    if portfolio_std <= 0:
        raise RiskXRayError(
            "Portfolio volatility is zero; contributions are undefined."
        )
    total_volatility = portfolio_std * math.sqrt(periods_per_year)

    rows: list[dict[str, object]] = []
    for index, symbol in enumerate(symbols):
        asset = matrix[index]
        asset_std = float(np.std(asset, ddof=1))
        if asset_std <= 0:
            # A constant series has no estimable covariance. Reporting 0.0 would
            # claim the holding contributed nothing; it contributed unmeasured.
            contribution = None
            share = None
        else:
            covariance = float(np.cov(asset, portfolio, ddof=1)[0, 1])
            contribution = (
                weight_vector[index] * covariance / portfolio_std
            ) * math.sqrt(periods_per_year)
            share = contribution / total_volatility * 100.0
            if not math.isfinite(contribution) or not math.isfinite(share):
                contribution = None
                share = None
        rows.append(
            {
                "symbol": symbol,
                "weight": float(weight_vector[index]),
                "risk_contribution": contribution,
                "contribution_pct": share,
            }
        )
    rows.sort(key=lambda item: -abs(_as_float(item["risk_contribution"])))
    return {
        "total_volatility_annualized": total_volatility,
        "contributions": rows,
        "periods_per_year": periods_per_year,
        "samples": length,
        "estimable": sum(1 for item in rows if item["risk_contribution"] is not None),
    }


def compute_risk_xray(
    equity: Sequence[float],
    returns: Mapping[str, Sequence[float]],
    weights: Mapping[str, float],
    *,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
    top: int = _MAX_EPISODES,
) -> dict[str, object]:
    """Drawdown episodes and volatility contributions in one payload."""
    return {
        "drawdown": drawdown_episodes(equity, top=top),
        "risk": risk_contributions(returns, weights, periods_per_year=periods_per_year),
    }


__all__ = [
    "DEFAULT_PERIODS_PER_YEAR",
    "DEFAULT_RECOVERY_RTOL",
    "RiskXRayError",
    "compute_risk_xray",
    "drawdown_episodes",
    "risk_contributions",
]
