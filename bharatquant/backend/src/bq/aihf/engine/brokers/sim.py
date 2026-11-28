"""SimBroker — deterministic simulated broker for backtests.

Fills every order completely. With a cost model, the fill price sits half a
spread plus the order's market impact away from the reference price and the
commission comes out of cash; without one, the fill is exactly at the
reference price. Either way the fill is deterministic: given the same
orders, a backtest replays to the same book.

Margin is not modeled: cash may go negative and stays visible. With an
unlevered mandate (gross_target <= 1), sells-before-buys ordering, and
floor-toward-zero sizing, a long book rarely gets there — the spread and
commission can take it a few dollars below zero when the share-rounding
dust is small — but nothing here pretends to enforce it.
"""

from __future__ import annotations

from math import isfinite

from bq.aihf.engine.brokers.costs import TradingCosts
from bq.aihf.engine.brokers.models import Fill, Order, Position


class SimBroker:
    """In-memory broker: signed positions plus a cash balance. *costs* is
    the mandate's cost model; None fills free, at the reference price.

    *shares* seeds an existing book — the approval gate's compute step rebuilds
    the fund's positions in memory so `advance` can be run without touching the
    live broker file.
    """

    def __init__(
        self,
        cash: float,
        costs: TradingCosts | None = None,
        shares: dict[str, int] | None = None,
    ) -> None:
        self._cash = cash
        self._costs = costs
        self._shares: dict[str, int] = {
            ticker: held for ticker, held in (shares or {}).items() if held != 0
        }

    def positions(self) -> dict[str, Position]:
        return {
            t: Position(ticker=t, shares=s)
            for t, s in self._shares.items()
            if s != 0
        }

    def cash(self) -> float:
        return self._cash

    def place_order(self, order: Order) -> Fill:
        if not isfinite(order.price) or order.price <= 0:
            raise ValueError(
                f"cannot fill {order.ticker} at price {order.price} — "
                "the caller must price every order"
            )

        price = order.price if self._costs is None else self._costs.fill_price(
            order.side, order.price, order.participation, order.volatility or 0.0,
        )
        # `charge` is a port hook (PATCHES.md): a cost model whose fees depend on
        # the notional — India's STT/exchange/SEBI/stamp/GST, each a percentage
        # of turnover — cannot be expressed as a per-share commission. A model
        # that does not implement it keeps upstream behaviour exactly.
        charge = 0.0 if self._costs is None else _charge_for(
            self._costs, order.side, order.quantity, order.price,
        )

        if order.side == "buy":
            self._shares[order.ticker] = self._shares.get(order.ticker, 0) + order.quantity
            self._cash -= order.quantity * price + charge
        else:
            self._shares[order.ticker] = self._shares.get(order.ticker, 0) - order.quantity
            self._cash += order.quantity * price - charge

        if self._shares[order.ticker] == 0:
            del self._shares[order.ticker]

        return Fill(
            ticker=order.ticker,
            side=order.side,
            quantity=order.quantity,
            price=price,
        )


def _charge_for(costs, side: str, quantity: int, price: float) -> float:
    """The notional-dependent fee for one fill.

    A cost model may implement ``charge(side, quantity, price) -> float``; the
    Indian schedule does, because STT/GST/stamp are percentages of turnover.
    Falls back to ``costs.commission(quantity)`` so every model written against
    upstream's interface behaves exactly as before.
    """
    charge = getattr(costs, "charge", None)
    if callable(charge):
        return float(charge(side, quantity, price))
    return float(costs.commission(quantity))
