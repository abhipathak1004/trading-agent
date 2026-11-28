"""Independent paper-trade reconciliation.

``PaperTradingStore.reconcile`` checks a book against its own writes. This checks
a *reported* state against the fills that supposedly produced it: a different
question, and the one that catches a hand-edited or drifted ledger.

Pure by construction — no broker, no network, no clock. Fees come from
:func:`bq.paper_charges.compute_charges` so there is exactly one fee formula in
the system, and every amount is integer paise so a one-rupee error is exactly
100, never ``99.99999``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from bq.paper_charges import ChargeSchedule, compute_charges

DEFAULT_TOLERANCE_PAISE: Final = 1
REPORTED_FIELDS: Final = (
    "cash_paise",
    "fees_paise",
    "realized_pnl_paise",
    "unrealized_pnl_paise",
    "nav_paise",
)


class ReconcileError(ValueError):
    """The ledger cannot be replayed as described."""


@dataclass(frozen=True, slots=True)
class FillRow:
    symbol: str
    side: str
    quantity: int
    price_paise: int
    product: str = "CAS"
    fees_paise: int | None = None


def _row(item: Mapping[str, object]) -> FillRow:
    fees = item.get("fees_paise")
    return FillRow(
        symbol=str(item["symbol"]),
        side=str(item["side"]),
        quantity=int(str(item["quantity"])),
        price_paise=int(str(item["price_paise"])),
        product=str(item.get("product", "CAS")),
        fees_paise=None if fees is None else int(str(fees)),
    )


def _position_state(
    fills: Sequence[FillRow],
    *,
    schedule: ChargeSchedule,
    venue: str,
) -> tuple[dict[str, int], dict[str, float], int, int, int]:
    quantities: dict[str, int] = {}
    average_cost: dict[str, float] = {}
    realized = 0.0
    fees_total = 0
    traded = 0
    for fill in fills:
        if fill.side not in {"BUY", "SELL"}:
            raise ReconcileError("A fill side must be BUY or SELL.")
        if fill.quantity <= 0 or fill.price_paise <= 0:
            raise ReconcileError("Fill quantity and price must be positive.")
        fees = fill.fees_paise
        if fees is None:
            fees = compute_charges(
                side=fill.side,
                product=fill.product,
                quantity=fill.quantity,
                price_paise=fill.price_paise,
                schedule=schedule,
                venue=venue,
            ).total_paise
        if fees < 0:
            raise ReconcileError("Fill fees must not be negative.")
        fees_total += fees
        notional = fill.quantity * fill.price_paise
        held = quantities.get(fill.symbol, 0)
        if fill.side == "BUY":
            average_cost[fill.symbol] = (
                average_cost.get(fill.symbol, 0.0) * held + notional
            ) / (held + fill.quantity)
            quantities[fill.symbol] = held + fill.quantity
            traded += notional
        else:
            if fill.quantity > held:
                raise ReconcileError(
                    f"Sell of {fill.symbol} exceeds the {held} units held."
                )
            realized += (fill.price_paise - average_cost[fill.symbol]) * fill.quantity
            quantities[fill.symbol] = held - fill.quantity
            traded -= notional
    return quantities, average_cost, int(round(realized)), fees_total, traded


def paper_trade_reconcile(
    initial_cash_paise: int,
    fills: Sequence[Mapping[str, object]],
    ending_prices: Mapping[str, int],
    reported_state: Mapping[str, object],
    *,
    tolerance_paise: int = DEFAULT_TOLERANCE_PAISE,
    schedule: ChargeSchedule | None = None,
    venue: str = "NSE",
) -> dict[str, object]:
    """Recompute cash, fees, P&L, positions and NAV from ``fills``.

    Returns ``{passed, discrepancies, computed_summary}``. ``passed`` is True only
    when every replayed field matches ``reported_state`` within the tolerance.
    """
    if initial_cash_paise <= 0:
        raise ReconcileError("Initial cash must be positive.")
    if tolerance_paise < 0:
        raise ReconcileError("Tolerance must not be negative.")
    rows = [_row(item) for item in fills]
    rates = schedule or ChargeSchedule.indian_equities()
    quantities, average_cost, realized, fees_total, traded = _position_state(
        rows, schedule=rates, venue=venue
    )

    cash = initial_cash_paise - traded - fees_total
    market_value = 0
    unrealized = 0.0
    positions: list[dict[str, object]] = []
    for symbol in sorted(quantities):
        quantity = quantities[symbol]
        if quantity <= 0:
            continue
        if symbol not in ending_prices:
            raise ReconcileError(f"No ending price for held symbol {symbol}.")
        mark = int(ending_prices[symbol])
        if mark <= 0:
            raise ReconcileError("Ending prices must be positive.")
        cost = average_cost[symbol]
        market_value += quantity * mark
        unrealized += (mark - cost) * quantity
        positions.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "average_cost_paise": round(cost),
                "mark_paise": mark,
                "market_value_paise": quantity * mark,
            }
        )
    unrealized_paise = int(round(unrealized))
    nav = cash + market_value

    computed: dict[str, object] = {
        "cash_paise": cash,
        "fees_paise": fees_total,
        "realized_pnl_paise": realized,
        "unrealized_pnl_paise": unrealized_paise,
        "nav_paise": nav,
        "positions": positions,
        "traded_paise": abs(traded),
    }

    discrepancies: list[dict[str, object]] = []
    for field in REPORTED_FIELDS:
        if field not in reported_state:
            continue
        expected = int(str(computed[field]))
        reported = int(str(reported_state[field]))
        difference = reported - expected
        if abs(difference) > tolerance_paise:
            discrepancies.append(
                {
                    "field": field,
                    "computed": expected,
                    "reported": reported,
                    "difference": difference,
                }
            )

    reported_positions = reported_state.get("positions")
    if isinstance(reported_positions, Mapping):
        replayed: dict[str, int] = {
            str(item["symbol"]): int(str(item["quantity"])) for item in positions
        }
        for symbol in sorted(set(replayed) | set(reported_positions)):
            expected_qty = replayed.get(symbol, 0)
            reported_qty = int(str(reported_positions.get(symbol, 0)))
            if expected_qty != reported_qty:
                discrepancies.append(
                    {
                        "field": f"positions.{symbol}",
                        "computed": expected_qty,
                        "reported": reported_qty,
                        "difference": reported_qty - expected_qty,
                    }
                )

    return {
        "passed": not discrepancies,
        "discrepancies": discrepancies,
        "computed_summary": computed,
    }


__all__ = [
    "DEFAULT_TOLERANCE_PAISE",
    "FillRow",
    "REPORTED_FIELDS",
    "ReconcileError",
    "paper_trade_reconcile",
]
