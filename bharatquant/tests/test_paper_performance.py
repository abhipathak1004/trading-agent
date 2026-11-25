from datetime import UTC, datetime

import pytest

from bq.paper_performance import (
    CapitalCashflow,
    PaperPerformanceError,
    time_weighted_return_bps,
    xirr_bps,
)


def test_time_weighted_return_links_subperiods() -> None:
    result = time_weighted_return_bps((100, -50))
    assert result.linked_subperiods == 2
    assert result.basis_points == 49


def test_time_weighted_return_rejects_below_minus_one_hundred() -> None:
    with pytest.raises(PaperPerformanceError, match="-100"):
        time_weighted_return_bps((-10_001,))


def test_xirr_simple_double_in_one_year() -> None:
    start = datetime(2025, 11, 23, tzinfo=UTC)
    end = datetime(2026, 11, 23, tzinfo=UTC)
    rate = xirr_bps(
        (CapitalCashflow(start, -10_000),),
        valuation_instant=end,
        terminal_value_paise=20_000,
    )
    assert abs(rate - 10_000) < 20
