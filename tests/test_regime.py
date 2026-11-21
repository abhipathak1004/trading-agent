"""A2 Regime tests — pure decision functions plus a synthetic full run."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from pinak.agents.regime import RegimeAgent, classify_regime, compute_verdict, write_report
from pinak.core.config import RegimeConfig
from pinak.core.errors import DataUnavailableError
from pinak.core.models import RegimeLabel, Verdict

END = date(2026, 11, 20)

INDEX_SCHEMA = {
    "index": pl.Utf8,
    "close": pl.Float64,
    "change_pct": pl.Float64,
    "date": pl.Date,
}
BAR_SCHEMA = {"symbol": pl.Utf8, "close": pl.Float64, "date": pl.Date}


def weekdays(end: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


class FakeIndices:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame

    def fetch(self, day: date) -> pl.DataFrame:
        out = self.frame.filter(pl.col("date") == day)
        if out.height == 0:
            raise DataUnavailableError(f"no indices for {day}")
        return out


class FakeBhavcopy:
    def __init__(self, frames: dict[date, pl.DataFrame]) -> None:
        self.frames = frames

    def fetch(self, day: date) -> pl.DataFrame:
        if day not in self.frames:
            raise DataUnavailableError(f"no bhavcopy for {day}")
        return self.frames[day]


def index_frame(days: list[date], closes: list[float], vix: float) -> pl.DataFrame:
    rows = []
    for day, close in zip(days, closes, strict=True):
        rows.append(
            {"index": "Nifty 50", "close": float(close), "change_pct": 0.0, "date": day}
        )
        rows.append(
            {"index": "India VIX", "close": float(vix), "change_pct": 0.0, "date": day}
        )
    return pl.DataFrame(rows, schema=INDEX_SCHEMA)


def bhavcopy_frames(days: list[date], advancing: int = 7, total: int = 10):
    previous_day, last_day = days[-2], days[-1]
    previous = pl.DataFrame(
        [
            {"symbol": f"S{i}", "close": 100.0, "date": previous_day}
            for i in range(total)
        ],
        schema=BAR_SCHEMA,
    )
    latest = pl.DataFrame(
        [
            {
                "symbol": f"S{i}",
                "close": 101.0 if i < advancing else 99.0,
                "date": last_day,
            }
            for i in range(total)
        ],
        schema=BAR_SCHEMA,
    )
    return {previous_day: previous, last_day: latest}


def make_agent(closes: list[float], vix: float = 14.0, advancing: int = 7) -> RegimeAgent:
    days = weekdays(END, len(closes))
    indices = FakeIndices(index_frame(days, closes, vix))
    bhavcopy = FakeBhavcopy(bhavcopy_frames(days, advancing=advancing))
    config = RegimeConfig(lookback_days=len(closes), sma_slow=200)
    return RegimeAgent(indices, bhavcopy, config)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("caution", "favorable", "verdict", "size", "positions"),
    [
        (0, 0, Verdict.YELLOW, 0.75, 2),
        (0, 1, Verdict.GREEN, 1.0, 4),
        (0, 2, Verdict.GREEN, 1.0, 5),
        (1, 0, Verdict.YELLOW, 0.5, 2),
        (1, 1, Verdict.YELLOW, 0.75, 3),
        (2, 0, Verdict.RED, 0.0, 0),
        (3, 2, Verdict.RED, 0.0, 0),
    ],
)
def test_verdict_table(caution, favorable, verdict, size, positions):
    assert compute_verdict(caution, favorable) == (verdict, size, positions)


@pytest.mark.parametrize(
    ("close", "fast", "slow", "ratio", "expected"),
    [
        (100.0, 90.0, 80.0, 1.0, RegimeLabel.BULL),
        (80.0, 90.0, 100.0, 1.0, RegimeLabel.BEAR),
        (100.0, 100.0, 100.0, 1.0, RegimeLabel.SIDEWAYS),
        (100.0, 90.0, 80.0, 1.6, RegimeLabel.HIGH_VOL),
    ],
)
def test_classify_regime(close, fast, slow, ratio, expected):
    assert classify_regime(close, fast, slow, ratio) is expected


def test_bull_trend_with_broad_participation_is_green():
    closes = [100.0 + i for i in range(220)]
    state = make_agent(closes, vix=14.0, advancing=7).run(END)

    assert state.label is RegimeLabel.BULL
    assert state.verdict is Verdict.GREEN
    assert state.size_multiplier == 1.0
    assert state.max_new_positions == 5
    assert state.breadth == pytest.approx(0.7)


def test_weak_breadth_downgrades_a_bull_day():
    closes = [100.0 + i for i in range(220)]
    state = make_agent(closes, vix=14.0, advancing=2).run(END)

    assert state.label is RegimeLabel.BULL
    assert state.verdict is Verdict.YELLOW
    assert state.size_multiplier == 0.75


def test_volatility_spike_and_high_vix_stand_the_day_down():
    closes = [100.0 + i * 0.1 for i in range(200)]
    for i in range(20):
        closes.append(closes[-1] * (1.05 if i % 2 == 0 else 0.95))

    state = make_agent(closes, vix=35.0).run(END)

    assert state.label is RegimeLabel.HIGH_VOL
    assert state.verdict is Verdict.RED
    assert state.size_multiplier == 0.0
    assert state.max_new_positions == 0


def test_report_is_written(tmp_path):
    state = make_agent([100.0 + i for i in range(220)]).run(END)
    json_path, markdown_path = write_report(state, tmp_path)

    assert json_path.exists()
    assert markdown_path.exists()
    assert "BULL" in markdown_path.read_text()
