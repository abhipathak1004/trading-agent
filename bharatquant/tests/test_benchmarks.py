import pytest

from bq.benchmarks import BenchmarkError, relative_budget


def test_relative_budget_allows_exactly_fifteen_percent() -> None:
    passed, regression = relative_budget(
        baseline_seconds=1.0,
        measured_seconds=1.15,
        allowed_regression_fraction=0.15,
    )
    assert passed is True
    assert regression == pytest.approx(0.15)


def test_relative_budget_flags_sixteen_percent_and_invalids() -> None:
    passed, regression = relative_budget(
        baseline_seconds=1.0,
        measured_seconds=1.16,
    )
    assert passed is False
    assert regression == pytest.approx(0.16)
    with pytest.raises(BenchmarkError, match="positive"):
        relative_budget(baseline_seconds=0.0, measured_seconds=1.0)
