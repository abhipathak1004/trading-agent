from dataclasses import dataclass
from datetime import datetime


class PaperPerformanceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CapitalCashflow:
    instant: datetime
    net_flow_paise: int


@dataclass(frozen=True, slots=True)
class TimeWeightedReturn:
    basis_points: int
    linked_subperiods: int


def time_weighted_return_bps(
    subperiod_returns: tuple[int, ...],
) -> TimeWeightedReturn:
    """Link caller-supplied subperiod decimal returns expressed as basis points."""
    if not isinstance(subperiod_returns, tuple) or len(subperiod_returns) > 10000:
        raise PaperPerformanceError("Subperiod returns must be a bounded tuple.")
    product = 10_000
    for value in subperiod_returns:
        if isinstance(value, bool) or not isinstance(value, int):
            raise PaperPerformanceError("Subperiod returns must be integers.")
        if value < -10_000:
            raise PaperPerformanceError("Subperiod return cannot be below -100%.")
        product = product * (10_000 + value) // 10_000
    return TimeWeightedReturn(
        basis_points=product - 10_000,
        linked_subperiods=len(subperiod_returns),
    )


def xirr_bps(
    cashflows: tuple[CapitalCashflow, ...],
    *,
    valuation_instant: datetime,
    terminal_value_paise: int,
) -> int:
    """Return a bounded decimal-year XIRR estimate expressed as basis points."""
    if not isinstance(cashflows, tuple) or len(cashflows) > 10000:
        raise PaperPerformanceError("Cashflows must be a bounded tuple.")
    if terminal_value_paise < 0:
        raise PaperPerformanceError("Terminal value cannot be negative.")
    if valuation_instant.utcoffset() is None:
        raise PaperPerformanceError("Valuation time must be timezone-aware.")

    dated: list[tuple[float, int]] = []
    start: datetime | None = None
    for item in cashflows:
        if not isinstance(item, CapitalCashflow):
            raise PaperPerformanceError("Invalid capital cashflow.")
        if item.instant.utcoffset() is None:
            raise PaperPerformanceError("Cashflow time must be timezone-aware.")
        if item.net_flow_paise == 0:
            raise PaperPerformanceError("Cashflow amount cannot be zero.")
        if start is None or item.instant < start:
            start = item.instant
        dated.append((item.instant.timestamp() / 86400.0, item.net_flow_paise))
    if start is None:
        raise PaperPerformanceError("At least one cashflow is required.")
    valuation_day = valuation_instant.timestamp() / 86400.0
    if valuation_day < max(day for day, _ in dated):
        raise PaperPerformanceError("Terminal value precedes a cashflow.")

    def npv(rate: float) -> float:
        base_day = start.timestamp() / 86400.0
        total = terminal_value_paise / (
            (1.0 + rate) ** ((valuation_day - base_day) / 365.0)
        )
        for day, amount in dated:
            total += amount / ((1.0 + rate) ** ((day - base_day) / 365.0))
        return total

    low = -0.999999
    high = 1000.0
    if npv(low) * npv(high) > 0:
        raise PaperPerformanceError("XIRR bracket does not contain a root.")
    for _ in range(200):
        middle = (low + high) / 2.0
        middle_value = npv(middle)
        if abs(middle_value) < 1e-8:
            low = high = middle
            break
        if npv(low) * middle_value <= 0:
            high = middle
        else:
            low = middle
    rate = (low + high) / 2.0
    if rate < -0.999999 or rate > 1000.0:
        raise PaperPerformanceError("XIRR is outside the supported bound.")
    return round(rate * 10_000)
