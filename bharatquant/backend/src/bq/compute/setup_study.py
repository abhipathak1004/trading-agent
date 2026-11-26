"""Deterministic historical study for confidence base rates.

This is deliberately small: one setup family, one horizon and one stop/target
geometry, computed from bars that were already known at each signal day. It is
not a strategy backtester and it never asks a model for a number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from bq.compute.indicators import compute_snapshot
from bq.data.vendors.base import Bar

MIN_BARS = 60
STOP_ATR = 1.5
TARGET_ATR = 2.5


class SetupStudyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SetupStudy:
    setup_id: str
    symbol: str
    horizon_days: int
    sample_count: int
    win_count: int
    win_rate: float
    data_as_of: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "horizon_days": self.horizon_days,
            "sample_count": self.sample_count,
            "win_count": self.win_count,
            "win_rate": self.win_rate,
            "data_as_of": self.data_as_of.isoformat(),
        }


def study_trend_continuation(
    symbol: str,
    bars: Sequence[Bar],
    *,
    horizon_days: int = 10,
) -> SetupStudy:
    if not isinstance(symbol, str) or not symbol:
        raise SetupStudyError("A symbol is required.")
    if (
        isinstance(horizon_days, bool)
        or not isinstance(horizon_days, int)
        or not 1 <= horizon_days <= 60
    ):
        raise SetupStudyError("The study horizon must be between 1 and 60 days.")
    if not isinstance(bars, Sequence) or len(bars) < MIN_BARS:
        raise SetupStudyError("The study needs at least 60 bars.")
    if isinstance(bars, (str, bytes)):
        raise SetupStudyError("Bars must be a sequence of Bar values.")
    if any(not isinstance(bar, Bar) for bar in bars):
        raise SetupStudyError("The study requires Bar values.")
    if any(
        bars[index].event_at >= bars[index + 1].event_at
        for index in range(len(bars) - 1)
    ):
        raise SetupStudyError("Study bars must be ordered by event time.")

    wins = 0
    samples = 0
    for index in range(MIN_BARS - 1, len(bars) - horizon_days - 1):
        snapshot = compute_snapshot(bars[: index + 1])
        if (
            snapshot.sma_20 is None
            or snapshot.sma_50 is None
            or snapshot.adx_14 is None
            or snapshot.rsi_14 is None
            or snapshot.atr_14 is None
            or snapshot.volume is None
            or snapshot.atr_14 <= 0
        ):
            continue
        if not (
            snapshot.close > snapshot.sma_20 > snapshot.sma_50
            and snapshot.adx_14 >= 20
            and 45 <= snapshot.rsi_14 <= 80
            and snapshot.volume.relative >= 1.2
        ):
            continue
        entry = float(bars[index + 1].open)
        stop = entry - STOP_ATR * snapshot.atr_14
        target = entry + TARGET_ATR * snapshot.atr_14
        outcome: int | None = None
        for bar in bars[index + 1 : index + horizon_days + 1]:
            low = float(bar.low)
            high = float(bar.high)
            if low <= stop:
                outcome = 0
                break
            if high >= target:
                outcome = 1
                break
        if outcome is None:
            outcome = 1 if float(bars[index + horizon_days].close) > entry else 0
        wins += outcome
        samples += 1

    return SetupStudy(
        setup_id="trend_continuation_v1",
        symbol=symbol,
        horizon_days=horizon_days,
        sample_count=samples,
        win_count=wins,
        win_rate=(wins / samples) if samples else 0.0,
        data_as_of=bars[-1].event_at,
    )


__all__ = [
    "MIN_BARS",
    "SetupStudy",
    "SetupStudyError",
    "STOP_ATR",
    "TARGET_ATR",
    "study_trend_continuation",
]
