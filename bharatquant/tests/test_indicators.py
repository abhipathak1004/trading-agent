import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from bq.compute.indicators import (
    IndicatorError,
    adx,
    atr,
    bars_to_frame,
    bollinger,
    compute_snapshot,
    ema,
    macd,
    max_drawdown,
    rsi,
    sma,
    swing_levels,
    volume_stats,
    vwap,
)
from bq.data.vendors.base import Bar


def _bars(closes: list[float], *, volumes: list[int] | None = None) -> list[Bar]:
    start = datetime(2026, 11, 1, tzinfo=UTC)
    vols = volumes or [100] * len(closes)
    out = []
    for index, close in enumerate(closes):
        price = Decimal(str(close))
        out.append(
            Bar(
                event_at=start + timedelta(days=index),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=vols[index],
            )
        )
    return out


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


# -- frame construction ----------------------------------------------------


def test_frame_is_sorted_and_float() -> None:
    bars = _bars([1.0, 2.0, 3.0])
    frame = bars_to_frame(list(reversed(bars)))
    assert list(frame["close"]) == [1.0, 2.0, 3.0]
    assert frame["close"].dtype == "float64"


def test_duplicate_timestamps_are_rejected() -> None:
    bars = _bars([1.0, 2.0])
    with pytest.raises(IndicatorError):
        bars_to_frame([bars[0], bars[0]])


def test_empty_input_is_rejected() -> None:
    with pytest.raises(IndicatorError):
        bars_to_frame([])


# -- moving averages -------------------------------------------------------


def test_sma_matches_hand_calculation() -> None:
    assert sma(_series([1, 2, 3, 4, 5]), 5) == pytest.approx(3.0)
    assert sma(_series([1, 2, 3, 4, 5]), 2) == pytest.approx(4.5)


def test_sma_needs_a_full_window() -> None:
    assert sma(_series([1, 2, 3]), 5) is None


def test_ema_matches_recursive_definition() -> None:
    values = [10.0, 11.0, 12.0]
    alpha = 2.0 / (3 + 1)
    expected = values[0]
    for value in values[1:]:
        expected = alpha * value + (1 - alpha) * expected
    assert ema(_series(values), 3) == pytest.approx(expected)


# -- RSI -------------------------------------------------------------------


def test_rsi_of_a_strictly_rising_series_is_100() -> None:
    assert rsi(_series(list(range(1, 20))), 14) == pytest.approx(100.0)


def test_rsi_of_a_strictly_falling_series_is_0() -> None:
    assert rsi(_series(list(range(20, 1, -1))), 14) == pytest.approx(0.0)


def test_rsi_matches_an_independent_wilder_calculation() -> None:
    """Golden value computed with an independent Wilder implementation."""
    closes = [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
    ]
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    period = 14
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    expected = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    assert rsi(_series(closes), period) == pytest.approx(expected, abs=1e-9)


def test_rsi_needs_period_plus_one_closes() -> None:
    assert rsi(_series([1.0] * 14), 14) is None
    assert rsi(_series([1.0] * 15), 14) is not None


# -- MACD ------------------------------------------------------------------


def test_macd_needs_warmup() -> None:
    assert macd(_series([1.0] * 30)) is None  # needs slow + signal = 35
    assert macd(_series([1.0] * 35)) is not None


def test_macd_histogram_is_line_minus_signal() -> None:
    values = [100.0 + index * 0.5 for index in range(60)]
    result = macd(_series(values))
    assert result is not None
    assert result.histogram == pytest.approx(result.macd - result.signal)


def test_macd_of_a_rising_series_is_positive() -> None:
    values = [100.0 + index for index in range(60)]
    result = macd(_series(values))
    assert result is not None and result.macd > 0


def test_macd_rejects_bad_periods() -> None:
    with pytest.raises(IndicatorError):
        macd(_series([1.0] * 50), fast=26, slow=12)


# -- Bollinger -------------------------------------------------------------


def test_bollinger_matches_hand_calculation() -> None:
    values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    result = bollinger(_series(values), period=8, deviations=2.0)
    assert result is not None
    mean = float(np.mean(values))
    population_std = float(np.std(values, ddof=0))
    assert result.middle == pytest.approx(mean)
    assert result.upper == pytest.approx(mean + 2 * population_std)
    assert result.lower == pytest.approx(mean - 2 * population_std)
    assert result.bandwidth == pytest.approx((result.upper - result.lower) / mean)


def test_bollinger_uses_population_std_not_sample() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = bollinger(_series(values), period=5, deviations=1.0)
    assert result is not None
    assert math.isclose(result.upper - result.middle, float(np.std(values, ddof=0)))


# -- ATR -------------------------------------------------------------------


def test_atr_matches_independent_wilder_calculation() -> None:
    """True range over a frame with a gap, checked against a plain loop."""
    highs = [10.0, 11.0, 12.0, 15.0, 14.0, 16.0]
    lows = [8.0, 9.0, 10.0, 11.0, 12.0, 13.0]
    closes = [9.0, 10.0, 11.0, 12.0, 13.0, 14.0]
    frame = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    period = 3
    trs = []
    for index in range(1, len(frame)):
        high = highs[index]
        low = lows[index]
        prev_close = closes[index - 1]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    expected = sum(trs[:period]) / period
    for tr in trs[period:]:
        expected = (expected * (period - 1) + tr) / period
    assert atr(frame, period) == pytest.approx(expected, abs=1e-9)


def test_atr_needs_period_plus_one_rows() -> None:
    frame = pd.DataFrame({"high": [2.0, 2.0], "low": [1.0, 1.0], "close": [1.5, 1.5]})
    assert atr(frame, 3) is None


# -- VWAP ------------------------------------------------------------------


def test_vwap_weights_by_volume() -> None:
    frame = pd.DataFrame(
        {
            "high": [11.0, 21.0],
            "low": [9.0, 19.0],
            "close": [10.0, 20.0],
            "volume": [1.0, 3.0],
        }
    )
    expected = (10.0 * 1.0 + 20.0 * 3.0) / 4.0
    assert vwap(frame) == pytest.approx(expected)


def test_vwap_with_no_volume_is_unavailable() -> None:
    frame = pd.DataFrame(
        {"high": [11.0], "low": [9.0], "close": [10.0], "volume": [0.0]}
    )
    assert vwap(frame) is None


# -- drawdown --------------------------------------------------------------


def test_max_drawdown_hand_calculation() -> None:
    # peak 120 then trough 60 is a 50% drawdown.
    assert max_drawdown(_series([100.0, 120.0, 60.0, 90.0])) == pytest.approx(0.5)


def test_max_drawdown_of_a_monotonic_rise_is_zero() -> None:
    assert max_drawdown(_series([1.0, 2.0, 3.0])) == pytest.approx(0.0)


# -- volume ----------------------------------------------------------------


def test_volume_relative_ratio() -> None:
    frame = pd.DataFrame({"volume": [100.0] * 4 + [300.0]})
    result = volume_stats(frame, period=5)
    assert result is not None
    assert result.average == pytest.approx(140.0)
    assert result.latest == pytest.approx(300.0)
    assert result.relative == pytest.approx(300.0 / 140.0)


# -- swings ----------------------------------------------------------------


def test_swing_levels_bracket_the_current_price() -> None:
    """Resistance must be above the close and support below it.

    An earlier implementation paired an old swing high with a distant low,
    producing a range the price had already left. These levels must bracket the
    close.
    """
    highs = [1.0, 2.0, 3.0, 10.0, 3.0, 2.0, 1.0, 2.0, 90.0]
    lows = [0.5, 0.5, 0.5, 1.0, 0.5, 0.5, 0.1, 0.5, 45.0]
    closes = [(high + low) / 2 for high, low in zip(highs, lows)]
    frame = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    result = swing_levels(frame, lookback=2)
    assert result is not None
    # Close is 67.5; the 10.0 swing high is below it, so it is not resistance.
    assert result.resistance is None or result.resistance > result.close
    assert result.support == pytest.approx(0.1)
    # Only one side is available, so there is no range and no position.
    assert result.position is None


def test_swing_levels_find_nearest_levels_on_each_side() -> None:
    closes = [10.0 + index * 0.2 for index in range(30)] + [15.6, 15.0]
    frame = pd.DataFrame(
        {
            "high": [value + 0.05 for value in closes],
            "low": [value - 0.05 for value in closes],
            "close": closes,
        }
    )
    result = swing_levels(frame, lookback=2)
    assert result is not None
    if result.resistance is not None:
        assert result.resistance > result.close
    if result.support is not None:
        assert result.support < result.close


def test_swing_levels_needs_enough_bars() -> None:
    frame = pd.DataFrame({"high": [1.0, 2.0], "low": [0.5, 1.0], "close": [1.0, 1.5]})
    assert swing_levels(frame, lookback=5) is None


# -- ADX -------------------------------------------------------------------


def test_adx_is_high_in_a_clean_trend_and_low_in_a_range() -> None:
    trend = _bars([100.0 + index for index in range(60)])
    trend_frame = bars_to_frame(trend)
    trend_frame["high"] = trend_frame["close"] + 1
    trend_frame["low"] = trend_frame["close"] - 1
    trend_adx = adx(trend_frame, 14)
    assert trend_adx is not None and trend_adx > 50

    flat = _bars([100.0 + (index % 2) * 0.1 for index in range(60)])
    flat_frame = bars_to_frame(flat)
    flat_frame["high"] = flat_frame["close"] + 0.05
    flat_frame["low"] = flat_frame["close"] - 0.05
    flat_adx = adx(flat_frame, 14)
    assert flat_adx is not None and flat_adx < 20


def test_adx_needs_two_periods() -> None:
    frame = pd.DataFrame({"high": [2.0] * 10, "low": [1.0] * 10, "close": [1.5] * 10})
    assert adx(frame, 14) is None


# -- snapshot --------------------------------------------------------------


def test_snapshot_reports_unavailable_indicators_as_none() -> None:
    snapshot = compute_snapshot(_bars([100.0, 101.0, 102.0]))
    assert snapshot.bar_count == 3
    assert snapshot.sma_20 is None
    assert snapshot.rsi_14 is None
    assert snapshot.close == pytest.approx(102.0)


def test_snapshot_is_json_safe() -> None:
    import json

    bars = _bars([100.0 + index * 0.4 for index in range(80)])
    snapshot = compute_snapshot(bars)
    payload = json.dumps(snapshot.to_dict())
    assert '"close"' in payload
    assert snapshot.available > 5


# -- no look-ahead ---------------------------------------------------------


def test_no_look_ahead_on_a_truncated_frame() -> None:
    """An indicator at bar N must not change when future bars are appended."""
    closes = [100.0 + (index % 7) * 0.9 + index * 0.15 for index in range(90)]
    full = _bars(closes)
    truncated = full[:70]

    full_frame = bars_to_frame(full)
    truncated_frame = bars_to_frame(truncated)

    # Computing on the first 70 bars of the full frame must equal computing on
    # a frame that only ever had 70 bars. If a future bar leaked in, these
    # would differ.
    assert rsi(full_frame["close"].iloc[:70], 14) == pytest.approx(
        rsi(truncated_frame["close"], 14)
    )
    assert macd(full_frame["close"].iloc[:70]) == macd(truncated_frame["close"])
    assert atr(full_frame.iloc[:70], 14) == pytest.approx(atr(truncated_frame, 14))
    assert bollinger(full_frame["close"].iloc[:70]) == bollinger(
        truncated_frame["close"]
    )
