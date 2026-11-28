"""Cross-sectional alpha benchmarking, including the survivorship stamp."""

import math
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from bq.research_lab.alpha_bench import AlphaBenchError, alpha_bench


def _synth_panel(
    *, symbols: int = 10, dates: int = 12
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Prices with a per-symbol drift; the factor is the exact next-day return."""
    days = [
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
        for index in range(dates)
    ]
    bar_rows: list[dict[str, object]] = []
    factor_rows: list[dict[str, object]] = []
    for index in range(symbols):
        symbol = f"S{index:02d}"
        drift = 1.0 + 0.01 * (index + 1)
        price = 10_000.0
        series: list[int] = []
        for _ in days:
            series.append(int(round(price)))
            price *= drift
        for position, day in enumerate(days):
            bar_rows.append({"symbol": symbol, "ts": day, "close": series[position]})
            if position + 1 < len(series):
                forward = series[position + 1] / series[position] - 1.0
                factor_rows.append(
                    {"symbol": symbol, "ts": day, "factor_value": forward}
                )
    bars = pl.DataFrame(bar_rows, schema_overrides={"ts": pl.Datetime("us", "UTC")})
    factor = pl.DataFrame(
        factor_rows, schema_overrides={"ts": pl.Datetime("us", "UTC")}
    )
    return bars, factor


def _finite_or_none(value: object) -> bool:
    return value is None or (isinstance(value, float) and math.isfinite(value))


def test_a_perfect_predictor_scores_one() -> None:
    bars, factor = _synth_panel()
    result = alpha_bench(bars, factor, horizons=(1,))
    (row,) = result["horizons"]
    assert row["ic_mean"] == pytest.approx(1.0)
    assert row["rank_ic_mean"] == pytest.approx(1.0)
    assert row["dates"] == 11


def test_results_are_finite_never_nan() -> None:
    bars, factor = _synth_panel()
    result = alpha_bench(bars, factor, horizons=(1, 5))
    for row in result["horizons"]:
        for field in ("ic_mean", "rank_ic_mean", "icir", "turnover", "mean_names"):
            assert _finite_or_none(row[field]), (field, row[field])


def test_survivorship_bias_is_stamped_for_current_membership() -> None:
    bars, factor = _synth_panel()
    current = alpha_bench(bars, factor)
    assert current["_meta"]["survivorship_bias_warning"] is True
    assert current["_meta"]["membership"] == "current"

    pit = alpha_bench(bars, factor, membership_is_current=False)
    assert pit["_meta"]["survivorship_bias_warning"] is False
    assert pit["_meta"]["membership"] == "point_in_time"


def test_meta_records_source_and_missing_symbols() -> None:
    bars, factor = _synth_panel(symbols=6)
    result = alpha_bench(
        bars,
        factor,
        horizons=(1,),
        source="equity_master",
        missing_symbols=("DELISTED",),
    )
    meta = result["_meta"]
    assert meta["source"] == "equity_master"
    assert meta["missing_symbols"] == ["DELISTED"]
    assert meta["universe_size"] == 6


def test_a_universe_too_small_to_rank_is_refused() -> None:
    bars, factor = _synth_panel(symbols=3)
    with pytest.raises(AlphaBenchError, match="minimum names"):
        alpha_bench(bars, factor, horizons=(1,))


def test_bad_horizons_and_missing_columns_are_refused() -> None:
    bars, factor = _synth_panel()
    with pytest.raises(AlphaBenchError):
        alpha_bench(bars, factor, horizons=(0,))
    with pytest.raises(AlphaBenchError):
        alpha_bench(bars, factor, horizons=(1, 1))
    with pytest.raises(AlphaBenchError):
        alpha_bench(bars.select(["symbol", "ts"]), factor)
    with pytest.raises(AlphaBenchError):
        alpha_bench(bars, factor.select(["symbol", "ts"]))
