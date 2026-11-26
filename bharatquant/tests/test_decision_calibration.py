import math

import pytest

from bq.decision.calibration import (
    CalibrationError,
    brier_score,
    expected_calibration_error,
    log_loss,
    reliability_curve,
    summarize,
)


def test_brier_log_loss_and_summary() -> None:
    pairs = [(0.8, 1), (0.2, 0)]
    assert brier_score(pairs) == pytest.approx(0.04)
    assert log_loss(pairs) == pytest.approx(-math.log(0.8))
    summary = summarize(pairs, bins=10)
    assert summary.count == 2
    assert summary.outcome_rate == 0.5
    assert summary.mean_brier == pytest.approx(0.04)
    assert summary.expected_calibration_error is not None


def test_reliability_curve_keeps_empty_bins() -> None:
    curve = reliability_curve([(0.95, 1), (0.95, 1)], bins=10)
    assert len(curve) == 10
    upper = curve[-1]
    assert upper.count == 2
    assert upper.mean_predicted == pytest.approx(0.95)
    assert upper.empirical == 1.0
    assert curve[0].count == 0
    assert curve[0].mean_predicted is None


def test_expected_calibration_error_is_the_weighted_gap() -> None:
    assert expected_calibration_error([(0.9, 1)], bins=10) == pytest.approx(0.1)


def test_empty_inputs_return_unavailable_metrics() -> None:
    assert brier_score([]) is None
    assert log_loss([]) is None
    assert expected_calibration_error([]) is None
    summary = summarize([])
    assert summary.count == 0
    assert summary.mean_brier is None


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(CalibrationError):
        brier_score([(1.5, 1)])
    with pytest.raises(CalibrationError):
        brier_score([(0.5, 2)])
    with pytest.raises(CalibrationError):
        brier_score([(True, 1)])
    with pytest.raises(CalibrationError):
        reliability_curve([(0.5, 1)], bins=0)
