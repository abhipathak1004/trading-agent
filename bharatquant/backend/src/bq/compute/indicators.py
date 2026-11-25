"""Deterministic technical indicators.

Every number the analyst agents see is computed here, in code. No model is ever
asked to calculate a moving average (plan §3, principle 11). ADR-014 records why
this uses pandas and numpy rather than stdlib arithmetic.

Conventions that matter for correctness:

* **Warm-up is explicit.** An indicator whose window has not filled returns
  ``None`` rather than a partially-seeded number or ``NaN``. A caller can always
  distinguish "not enough history" from "a real value".
* **Wilder smoothing is seeded correctly.** RSI and ATR use an initial simple
  average over the first ``period`` observations, then Wilder's recursive form.
  Seeding them with the first single observation is a common bug that makes the
  first few values meaningless.
* **No look-ahead.** Every function only reads rows up to the index it reports
  on. ``tests/test_indicators.py`` checks a truncated frame against the full one.
* **Prices stay float64.** pandas produces float64; that is fine for indicator
  maths. The one conversion to exact paise happens in the analysis layer when a
  price becomes a trade-plan field, and it is asserted there.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from bq.data.vendors.base import Bar

DEFAULT_RSI_PERIOD = 14
DEFAULT_ATR_PERIOD = 14
DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9
DEFAULT_BOLLINGER_PERIOD = 20
DEFAULT_BOLLINGER_DEVIATIONS = 2.0
DEFAULT_ADX_PERIOD = 14
DEFAULT_SWING_LOOKBACK = 5

# Minimum bars below which an indicator is reported unavailable rather than
# computed from a window that is too short to mean anything.
MIN_BARS_FOR_ANY_INDICATOR = 2


class IndicatorError(ValueError):
    """Raised when input cannot produce an indicator at all."""


def bars_to_frame(bars: Sequence[Bar]) -> pd.DataFrame:
    """Turn vendor bars into an ordered float64 frame.

    Duplicate timestamps are rejected rather than silently de-duplicated: two
    bars for the same instant means the caller mixed sources, and averaging or
    picking one would hide that.
    """
    if not bars:
        raise IndicatorError("At least one bar is required.")
    rows = [
        {
            "event_at": bar.event_at,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
        }
        for bar in bars
    ]
    frame = pd.DataFrame(rows).sort_values("event_at", kind="stable")
    if frame["event_at"].duplicated().any():
        raise IndicatorError("Duplicate bar timestamps are not allowed.")
    frame = frame.reset_index(drop=True)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = frame[column].astype("float64")
    return frame


def _last_valid(series: pd.Series) -> float | None:
    if series.empty:
        return None
    value = series.iloc[-1]
    if pd.isna(value):
        return None
    return float(value)


def sma(close: pd.Series, period: int = 20) -> float | None:
    """Simple moving average of the last ``period`` closes."""
    if period < 1:
        raise IndicatorError("Period must be positive.")
    if len(close) < period:
        return None
    return _last_valid(close.rolling(window=period).mean())


def ema(close: pd.Series, period: int = 20) -> float | None:
    """Exponential moving average, ``adjust=False`` so it is price-recursive."""
    if period < 1:
        raise IndicatorError("Period must be positive.")
    if len(close) < period:
        return None
    return _last_valid(close.ewm(span=period, adjust=False).mean())


def rsi(close: pd.Series, period: int = DEFAULT_RSI_PERIOD) -> float | None:
    """Wilder's Relative Strength Index.

    Needs ``period + 1`` closes because it is computed from ``period`` deltas.
    A flat series has no losses, so RSI is 100 by convention rather than a
    division by zero.
    """
    if period < 1:
        raise IndicatorError("Period must be positive.")
    if len(close) < period + 1:
        return None
    delta = close.diff().iloc[1:]
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    avg_gain = float(gains.iloc[:period].mean())
    avg_loss = float(losses.iloc[:period].mean())
    for index in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + float(gains.iloc[index])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses.iloc[index])) / period
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass(frozen=True, slots=True)
class Macd:
    macd: float
    signal: float
    histogram: float


def macd(
    close: pd.Series,
    fast: int = DEFAULT_MACD_FAST,
    slow: int = DEFAULT_MACD_SLOW,
    signal: int = DEFAULT_MACD_SIGNAL,
) -> Macd | None:
    """MACD line, signal line and histogram.

    Needs ``slow + signal`` closes for the signal EMA to be meaningful.
    """
    if not 0 < fast < slow:
        raise IndicatorError("MACD requires 0 < fast < slow.")
    if signal < 1:
        raise IndicatorError("Signal period must be positive.")
    if len(close) < slow + signal:
        return None
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    line = fast_ema - slow_ema
    signal_line = line.ewm(span=signal, adjust=False).mean()
    macd_value = _last_valid(line)
    signal_value = _last_valid(signal_line)
    if macd_value is None or signal_value is None:
        return None
    return Macd(
        macd=macd_value,
        signal=signal_value,
        histogram=macd_value - signal_value,
    )


@dataclass(frozen=True, slots=True)
class Bollinger:
    middle: float
    upper: float
    lower: float
    bandwidth: float


def bollinger(
    close: pd.Series,
    period: int = DEFAULT_BOLLINGER_PERIOD,
    deviations: float = DEFAULT_BOLLINGER_DEVIATIONS,
) -> Bollinger | None:
    """Bollinger Bands using the population standard deviation.

    ``ddof=0`` matches the usual charting convention. ``bandwidth`` is
    ``(upper - lower) / middle``, so it is comparable across price levels.
    """
    if period < 2:
        raise IndicatorError("Bollinger period must be at least 2.")
    if deviations <= 0:
        raise IndicatorError("Deviation multiplier must be positive.")
    if len(close) < period:
        return None
    window = close.iloc[-period:]
    middle = float(window.mean())
    spread = float(window.std(ddof=0))
    upper = middle + deviations * spread
    lower = middle - deviations * spread
    return Bollinger(
        middle=middle,
        upper=upper,
        lower=lower,
        bandwidth=(upper - lower) / middle if middle else 0.0,
    )


def atr(frame: pd.DataFrame, period: int = DEFAULT_ATR_PERIOD) -> float | None:
    """Wilder's Average True Range.

    True range accounts for gaps: ``max(high-low, |high-prev_close|,
    |low-prev_close|)``. Needs ``period + 1`` rows.
    """
    if period < 1:
        raise IndicatorError("Period must be positive.")
    if len(frame) < period + 1:
        return None
    high = frame["high"]
    low = frame["low"]
    prev_close = frame["close"].shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    true_range = ranges.iloc[1:]
    value = float(true_range.iloc[:period].mean())
    for index in range(period, len(true_range)):
        value = (value * (period - 1) + float(true_range.iloc[index])) / period
    return value


def vwap(frame: pd.DataFrame) -> float | None:
    """Volume-weighted average price over the whole frame.

    Uses the typical price ``(h + l + c) / 3``. Returns ``None`` when total
    volume is zero, because a VWAP with no volume is not a price.
    """
    if frame.empty:
        return None
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    volume = frame["volume"]
    total = float(volume.sum())
    if total <= 0:
        return None
    return float((typical * volume).sum() / total)


def adx(frame: pd.DataFrame, period: int = DEFAULT_ADX_PERIOD) -> float | None:
    """Wilder's Average Directional Index, a trend-strength measure.

    Values below ~20 mean a range, above ~25 a trend. Needs ``2 * period`` rows
    because both the smoothed DX and its average have to fill.
    """
    if period < 1:
        raise IndicatorError("Period must be positive.")
    if len(frame) < 2 * period:
        return None
    high = frame["high"]
    low = frame["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_raw = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_raw = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_raw, index=frame.index).iloc[1:]
    minus_dm = pd.Series(minus_raw, index=frame.index).iloc[1:]

    prev_close = frame["close"].shift(1)
    true_range = (
        pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        )
        .max(axis=1)
        .iloc[1:]
    )

    def _wilder(series: pd.Series) -> pd.Series:
        return series.ewm(alpha=1.0 / period, adjust=False).mean()

    atr_smooth = _wilder(true_range)
    plus_di = 100.0 * _wilder(plus_dm) / atr_smooth.replace(0.0, np.nan)
    minus_di = 100.0 * _wilder(minus_dm) / atr_smooth.replace(0.0, np.nan)
    denominator = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    return _last_valid(_wilder(dx.dropna()))


def max_drawdown(close: pd.Series) -> float | None:
    """Largest peak-to-trough decline, as a positive fraction (0.15 = 15%)."""
    if len(close) < 2:
        return None
    running_peak = close.cummax()
    drawdown = (running_peak - close) / running_peak.replace(0.0, np.nan)
    value = drawdown.max()
    if pd.isna(value):
        return None
    return float(value)


@dataclass(frozen=True, slots=True)
class VolumeStats:
    latest: float
    average: float
    relative: float


def volume_stats(frame: pd.DataFrame, period: int = 20) -> VolumeStats | None:
    """Latest volume against its trailing average.

    ``relative`` above 1 means heavier-than-usual participation, which is the
    useful signal for a breakout claim.
    """
    if period < 1:
        raise IndicatorError("Period must be positive.")
    if len(frame) < period:
        return None
    window = frame["volume"].iloc[-period:]
    average = float(window.mean())
    latest = float(window.iloc[-1])
    if average <= 0:
        return None
    return VolumeStats(latest=latest, average=average, relative=latest / average)


@dataclass(frozen=True, slots=True)
class SwingLevels:
    """Structural support and resistance from recent swing points.

    Both levels are defined *relative to the current close*: resistance is the
    nearest confirmed swing high **above** price, support the nearest confirmed
    swing low **below** it. A swing high that price has already cleared is no
    longer resistance, and pairing it with a distant low produced a range the
    price sat outside of. Either side may be ``None`` when no confirmed swing
    exists on that side.
    """

    support: float | None
    resistance: float | None
    recent_high: float
    recent_low: float
    close: float

    @property
    def position(self) -> float | None:
        """Where the close sits in the range (0 = at support, 1 = at resistance)."""
        if self.support is None or self.resistance is None:
            return None
        span = self.resistance - self.support
        if span <= 0:
            return None
        return (self.close - self.support) / span


def swing_levels(
    frame: pd.DataFrame, lookback: int = DEFAULT_SWING_LOOKBACK
) -> SwingLevels | None:
    """Nearest confirmed swing high above price and swing low below it.

    A swing high is a high greater than ``lookback`` bars on each side; a swing
    low is the mirror. The search walks backwards from the most recent bar, so the
    first qualifying level found is the nearest one. A level on the wrong side of
    the current close is skipped, because price has already traded through it.
    """
    if lookback < 1:
        raise IndicatorError("Lookback must be positive.")
    if len(frame) < 2 * lookback + 1:
        return None
    highs = frame["high"]
    lows = frame["low"]
    close = float(frame["close"].iloc[-1])
    swing_high: float | None = None
    swing_low: float | None = None
    for index in range(len(frame) - lookback - 1, lookback - 1, -1):
        candidate_high = float(highs.iloc[index])
        candidate_low = float(lows.iloc[index])
        if swing_high is None and candidate_high > close:
            window_highs = highs.iloc[index - lookback : index + lookback + 1]
            if candidate_high >= window_highs.max():
                swing_high = candidate_high
        if swing_low is None and candidate_low < close:
            window_lows = lows.iloc[index - lookback : index + lookback + 1]
            if candidate_low <= window_lows.min():
                swing_low = candidate_low
        if swing_high is not None and swing_low is not None:
            break
    return SwingLevels(
        support=swing_low,
        resistance=swing_high,
        recent_high=float(highs.iloc[-1]),
        recent_low=float(lows.iloc[-1]),
        close=close,
    )


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    """Every indicator at one point in time, with explicit availability."""

    as_of: datetime
    bar_count: int
    close: float
    sma_20: float | None
    sma_50: float | None
    ema_20: float | None
    rsi_14: float | None
    macd: Macd | None
    bollinger: Bollinger | None
    atr_14: float | None
    vwap: float | None
    adx_14: float | None
    max_drawdown: float | None
    volume: VolumeStats | None
    swings: SwingLevels | None

    @property
    def available(self) -> int:
        """How many indicators had enough history to be computed."""
        return sum(
            1
            for value in (
                self.sma_20,
                self.sma_50,
                self.ema_20,
                self.rsi_14,
                self.macd,
                self.bollinger,
                self.atr_14,
                self.vwap,
                self.adx_14,
                self.max_drawdown,
                self.volume,
                self.swings,
            )
            if value is not None
        )

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe view. Used for the run audit and the UI."""
        return {
            "as_of": self.as_of.isoformat(),
            "bar_count": self.bar_count,
            "close": self.close,
            "sma_20": self.sma_20,
            "sma_50": self.sma_50,
            "ema_20": self.ema_20,
            "rsi_14": self.rsi_14,
            "macd": None
            if self.macd is None
            else {
                "macd": self.macd.macd,
                "signal": self.macd.signal,
                "histogram": self.macd.histogram,
            },
            "bollinger": None
            if self.bollinger is None
            else {
                "middle": self.bollinger.middle,
                "upper": self.bollinger.upper,
                "lower": self.bollinger.lower,
                "bandwidth": self.bollinger.bandwidth,
            },
            "atr_14": self.atr_14,
            "vwap": self.vwap,
            "adx_14": self.adx_14,
            "max_drawdown": self.max_drawdown,
            "volume": None
            if self.volume is None
            else {
                "latest": self.volume.latest,
                "average": self.volume.average,
                "relative": self.volume.relative,
            },
            "swings": None
            if self.swings is None
            else {
                "support": self.swings.support,
                "resistance": self.swings.resistance,
                "recent_high": self.swings.recent_high,
                "recent_low": self.swings.recent_low,
                "position": self.swings.position,
            },
        }


def compute_snapshot(bars: Sequence[Bar]) -> IndicatorSnapshot:
    """Compute every indicator for the given bars, as of the last bar."""
    frame = bars_to_frame(bars)
    close = frame["close"]
    return IndicatorSnapshot(
        as_of=frame["event_at"].iloc[-1].to_pydatetime(),
        bar_count=len(frame),
        close=float(close.iloc[-1]),
        sma_20=sma(close, 20),
        sma_50=sma(close, 50),
        ema_20=ema(close, 20),
        rsi_14=rsi(close),
        macd=macd(close),
        bollinger=bollinger(close),
        atr_14=atr(frame),
        vwap=vwap(frame),
        adx_14=adx(frame),
        max_drawdown=max_drawdown(close),
        volume=volume_stats(frame),
        swings=swing_levels(frame),
    )


__all__ = [
    "DEFAULT_ATR_PERIOD",
    "DEFAULT_RSI_PERIOD",
    "DEFAULT_SWING_LOOKBACK",
    "IndicatorError",
    "IndicatorSnapshot",
    "MIN_BARS_FOR_ANY_INDICATOR",
    "Bollinger",
    "Macd",
    "SwingLevels",
    "VolumeStats",
    "adx",
    "atr",
    "bars_to_frame",
    "bollinger",
    "compute_snapshot",
    "ema",
    "macd",
    "max_drawdown",
    "rsi",
    "sma",
    "swing_levels",
    "volume_stats",
    "vwap",
]
