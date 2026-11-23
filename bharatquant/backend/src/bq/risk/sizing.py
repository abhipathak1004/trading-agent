from dataclasses import dataclass

from bq.core import CurrencyPaise, Quantity

BASIS_POINTS = 10_000


@dataclass(frozen=True, slots=True)
class FixedFractionalSize:
    risk_budget: CurrencyPaise
    risk_per_share: CurrencyPaise
    risk_limited_quantity: Quantity
    cash_limited_quantity: Quantity
    quantity: Quantity


def size_long_fixed_fractional(
    *,
    equity: CurrencyPaise,
    available_cash: CurrencyPaise,
    entry_price: CurrencyPaise,
    stop_price: CurrencyPaise,
    risk_basis_points: int,
) -> FixedFractionalSize:
    if not all(
        isinstance(value, CurrencyPaise)
        for value in (equity, available_cash, entry_price, stop_price)
    ):
        raise TypeError("Sizing amounts must be CurrencyPaise values.")
    if (
        isinstance(risk_basis_points, bool)
        or not isinstance(risk_basis_points, int)
        or not 0 <= risk_basis_points <= BASIS_POINTS
    ):
        raise ValueError("Risk basis points must be an integer from 0 to 10000.")
    if stop_price.value <= 0 or entry_price.value <= stop_price.value:
        raise ValueError("A long stop must be positive and below the entry price.")
    risk_budget = equity.value * risk_basis_points // BASIS_POINTS
    risk_per_share = entry_price.value - stop_price.value
    risk_limited = risk_budget // risk_per_share
    cash_limited = available_cash.value // entry_price.value
    return FixedFractionalSize(
        risk_budget=CurrencyPaise(risk_budget),
        risk_per_share=CurrencyPaise(risk_per_share),
        risk_limited_quantity=Quantity(risk_limited),
        cash_limited_quantity=Quantity(cash_limited),
        quantity=Quantity(min(risk_limited, cash_limited)),
    )
