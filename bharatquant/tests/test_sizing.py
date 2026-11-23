import pytest

from bq.core import CurrencyPaise, Quantity
from bq.risk.sizing import size_long_fixed_fractional


def test_long_size_is_limited_by_risk_and_cash_in_whole_shares() -> None:
    size = size_long_fixed_fractional(
        equity=CurrencyPaise(1_000_001),
        available_cash=CurrencyPaise(95_000),
        entry_price=CurrencyPaise(10_000),
        stop_price=CurrencyPaise(9_000),
        risk_basis_points=100,
    )
    assert size.risk_budget == CurrencyPaise(10_000)
    assert size.risk_per_share == CurrencyPaise(1_000)
    assert size.risk_limited_quantity == Quantity(10)
    assert size.cash_limited_quantity == Quantity(9)
    assert size.quantity == Quantity(9)


def test_long_size_can_be_risk_limited_or_zero() -> None:
    inputs = {
        "equity": CurrencyPaise(1_000_000),
        "available_cash": CurrencyPaise(1_000_000),
        "entry_price": CurrencyPaise(10_000),
        "stop_price": CurrencyPaise(9_000),
    }
    assert size_long_fixed_fractional(
        **inputs, risk_basis_points=50
    ).quantity == Quantity(5)
    assert size_long_fixed_fractional(
        **inputs, risk_basis_points=0
    ).quantity == Quantity(0)
    inputs["available_cash"] = CurrencyPaise(9_999)
    assert size_long_fixed_fractional(
        **inputs, risk_basis_points=100
    ).quantity == Quantity(0)


@pytest.mark.parametrize("basis_points", [-1, 10_001, True, 1.5])
def test_invalid_risk_fraction_is_rejected(basis_points: int) -> None:
    with pytest.raises(ValueError, match="basis points"):
        size_long_fixed_fractional(
            equity=CurrencyPaise(100),
            available_cash=CurrencyPaise(100),
            entry_price=CurrencyPaise(10),
            stop_price=CurrencyPaise(5),
            risk_basis_points=basis_points,
        )


@pytest.mark.parametrize("stop", [0, 100, 101])
def test_invalid_long_stop_is_rejected(stop: int) -> None:
    with pytest.raises(ValueError, match="stop"):
        size_long_fixed_fractional(
            equity=CurrencyPaise(100),
            available_cash=CurrencyPaise(100),
            entry_price=CurrencyPaise(100),
            stop_price=CurrencyPaise(stop),
            risk_basis_points=100,
        )


def test_large_values_are_calculated_without_float_rounding() -> None:
    size = size_long_fixed_fractional(
        equity=CurrencyPaise(10**30 + 1),
        available_cash=CurrencyPaise(10**30),
        entry_price=CurrencyPaise(3),
        stop_price=CurrencyPaise(1),
        risk_basis_points=333,
    )
    assert size.risk_budget.value == (10**30 + 1) * 333 // 10_000
    assert size.quantity.value == size.risk_budget.value // 2


def test_sizing_never_exceeds_either_supplied_limit() -> None:
    for equity in (0, 100, 10_000):
        for cash in (0, 99, 500, 10_000):
            for basis_points in (0, 125, 10_000):
                result = size_long_fixed_fractional(
                    equity=CurrencyPaise(equity),
                    available_cash=CurrencyPaise(cash),
                    entry_price=CurrencyPaise(100),
                    stop_price=CurrencyPaise(73),
                    risk_basis_points=basis_points,
                )
                quantity = result.quantity.value
                assert quantity * 27 <= result.risk_budget.value
                assert quantity * 100 <= cash
                assert (quantity + 1) * 27 > result.risk_budget.value or (
                    quantity + 1
                ) * 100 > cash
