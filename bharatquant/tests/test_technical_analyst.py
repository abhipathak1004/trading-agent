from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bq.analysis.technical import (
    SubScore,
    TechnicalAnalyst,
    TechnicalReport,
)
from bq.data.vendors.base import Bar

START = datetime(2026, 6, 1, tzinfo=UTC)


def _bars(closes: list[float], *, spread: float = 0.5, volume: int = 100) -> list[Bar]:
    out = []
    for index, close in enumerate(closes):
        price = Decimal(str(round(close, 4)))
        half = Decimal(str(spread))
        out.append(
            Bar(
                event_at=START + timedelta(days=index),
                open=price,
                high=price + half,
                low=max(price - half, Decimal("0.01")),
                close=price,
                volume=volume,
            )
        )
    return out


def _uptrend(count: int = 120) -> list[Bar]:
    return _bars([100.0 + index * 0.8 for index in range(count)])


def _downtrend(count: int = 120) -> list[Bar]:
    return _bars([200.0 - index * 0.8 for index in range(count)])


# -- sub-score invariants --------------------------------------------------


def test_sub_score_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError):
        SubScore(value=1.5, evidence=("x",))


def test_sub_score_requires_evidence() -> None:
    with pytest.raises(ValueError):
        SubScore(value=0.0, evidence=())


# -- trend -----------------------------------------------------------------


def test_uptrend_scores_positive_trend() -> None:
    report = TechnicalAnalyst().analyse(
        symbol="NSE:RELIANCE", bars=_uptrend(), timeframe="swing"
    )
    assert report.trend is not None
    assert report.trend.value > 0
    assert any("above" in line for line in report.trend.evidence)


def test_downtrend_scores_negative_trend() -> None:
    report = TechnicalAnalyst().analyse(
        symbol="NSE:RELIANCE", bars=_downtrend(), timeframe="swing"
    )
    assert report.trend is not None
    assert report.trend.value < 0


def test_momentum_of_a_rising_series_is_positive() -> None:
    report = TechnicalAnalyst().analyse(
        symbol="NSE:TCS", bars=_uptrend(), timeframe="swing"
    )
    assert report.momentum is not None
    assert report.momentum.value > 0


# -- availability ----------------------------------------------------------


def test_short_history_reports_dimensions_unavailable() -> None:
    report = TechnicalAnalyst().analyse(
        symbol="NSE:TCS", bars=_bars([100.0, 101.0, 102.0]), timeframe="intraday"
    )
    assert report.trend is None
    assert report.momentum is None
    assert report.available_dimensions == 0
    assert report.composite is None
    # Unavailable is stated, not silently rendered as neutral.
    assert any("unavailable" in line for line in report.facts())


def test_composite_is_the_mean_of_available_dimensions() -> None:
    report = TechnicalAnalyst().analyse(
        symbol="NSE:TCS", bars=_uptrend(), timeframe="swing"
    )
    scores = [score.value for score in report.scored_dimensions]
    assert report.composite == pytest.approx(sum(scores) / len(scores))


def test_sub_scores_are_bounded() -> None:
    report = TechnicalAnalyst().analyse(
        symbol="NSE:TCS", bars=_uptrend(), timeframe="swing"
    )
    for score in report.scored_dimensions:
        assert -1.0 <= score.value <= 1.0


# -- look-ahead ------------------------------------------------------------


def test_bars_after_as_of_are_dropped() -> None:
    bars = _uptrend(120)
    cutoff = bars[80].event_at
    report = TechnicalAnalyst().analyse(
        symbol="NSE:RELIANCE", bars=bars, timeframe="swing", as_of=cutoff
    )
    assert report.bar_count == 81
    assert report.as_of == cutoff
    assert report.indicators.as_of == cutoff


def test_as_of_before_every_bar_is_refused() -> None:
    bars = _uptrend(30)
    with pytest.raises(ValueError):
        TechnicalAnalyst().analyse(
            symbol="NSE:RELIANCE",
            bars=bars,
            timeframe="swing",
            as_of=bars[0].event_at - timedelta(days=1),
        )


def test_future_bars_do_not_change_a_past_report() -> None:
    bars = _uptrend(120)
    cutoff = bars[90].event_at
    with_future = TechnicalAnalyst().analyse(
        symbol="NSE:RELIANCE", bars=bars, timeframe="swing", as_of=cutoff
    )
    without_future = TechnicalAnalyst().analyse(
        symbol="NSE:RELIANCE", bars=bars[:91], timeframe="swing"
    )
    assert with_future.composite == pytest.approx(without_future.composite)
    assert with_future.bar_count == without_future.bar_count


# -- output ----------------------------------------------------------------


def test_report_is_json_safe() -> None:
    import json

    report = TechnicalAnalyst().analyse(
        symbol="NSE:RELIANCE", bars=_uptrend(), timeframe="swing"
    )
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["symbol"] == "NSE:RELIANCE"
    assert isinstance(payload["facts"], list)
    assert Decimal(str(payload["last_close"])) == report.last_close


def test_empty_bars_are_rejected() -> None:
    with pytest.raises(ValueError):
        TechnicalAnalyst().analyse(symbol="NSE:X", bars=[], timeframe="swing")


def test_report_contains_no_model_output() -> None:
    """The report is numbers and deterministic sentences only."""
    report: TechnicalReport = TechnicalAnalyst().analyse(
        symbol="NSE:RELIANCE", bars=_uptrend(), timeframe="swing"
    )
    for line in report.facts():
        # Every evidence line must mention a number, i.e. be traceable to math.
        assert any(char.isdigit() for char in line)
