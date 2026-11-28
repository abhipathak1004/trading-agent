"""The cost model an NSE equity mandate actually pays.

Upstream's ``TradingCosts`` is a US model: a half-spread in basis points, a
per-share commission (Interactive Brokers' fixed rate) and a square-root market
impact. None of those lines exists for an Indian delivery trade, and the fees
that do exist are all percentages of turnover, which a per-share commission
cannot express.

This module supplies a cost model the engine accepts unchanged (it implements
``fill_price``, ``impact`` and ``commission``) but which prices fills with
``bq.paper_charges.compute_charges`` — the same function the paper-trading ledger
uses, so a fund and the rest of the app charge identically:

    STT            0.1% on delivery, both sides
    exchange       0.00297% of turnover
    SEBI           0.0001% of turnover
    stamp duty     0.015% on the buy side only
    GST            18% on (exchange + SEBI)

Two deliberate choices:

* **The half-spread stays a configurable slippage term.** Upstream charges 5 bps
  of half-spread *and* square-root impact; for a T+1 delivery book over daily
  closes the impact term is not defensible without tick data, so it is off by
  default and the spread survives as the honest "I do not know the touch" cost.
* **Charges are returned in rupees, not paise.** The engine's book is float
  rupees (its marks are float prices), so the paise a `ChargeBreakdown` reports
  are converted at the boundary; `_charge_for` in `brokers/sim.py` sums it into
  cash. The paper charges module stays the single source of rates.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import PrivateAttr

from bq.aihf.engine.brokers.costs import TradingCosts
from bq.paper_charges import ChargeSchedule, compute_charges

# Delivery (CNC) product: a swing mandate settles T+1 and pays delivery STT.
DELIVERY_PRODUCT = "CAS"

# Rounded to the paise before converting to rupees: the schedule is exact in
# integer paise, and doing the division last keeps it that way.
_PAISE_PER_RUPEE = 100


def _price_paise(price: float) -> int:
    """An engine float price as integer paise.

    The engine works in float rupees (its marks are floats from the data
    client). Rounding half-up at the paise boundary is the same convention
    `bq.core.paise_from_price` uses for the rest of the app; a price that
    rounds to zero (under half a paisa) is a data error, not a trade.
    """
    paise = int((Decimal(str(price)) * _PAISE_PER_RUPEE).quantize(Decimal("1")))
    if paise <= 0:
        raise ValueError(f"a fill price must be at least one paisa, got {price!r}")
    return paise


class IndianEquityCosts(TradingCosts):
    """A ``TradingCosts`` that charges Indian delivery fees on turnover.

    Subclasses the upstream model so it satisfies the engine's `costs` typing
    (``FundSpec.costs``) and inherits `fill_price`/`impact` unchanged, then
    overrides the two fee methods the notional-dependent schedule needs.

    Fields that upstream's defaults carry but that do not apply here are set to
    zero: delivery is free of per-share commission, and impact needs tick data
    this app does not have. The half-spread remains the one slippage term.

    ``_rate_schedule`` is a pydantic private attribute, not a model field: it
    pins exact rates in a test without becoming part of the mandate that is
    serialized and hashed into the ledger.
    """

    _rate_schedule: ChargeSchedule | None = PrivateAttr(default=None)

    def commission(self, quantity: int) -> float:  # noqa: ARG002 - signature fixed upstream
        """Zero: an Indian delivery trade has no per-share commission line.

        The real fees are turnover percentages and are applied by `charge`,
        which the engine calls with the fill's notional.
        """
        return 0.0

    def charge(self, side: str, quantity: int, price: float) -> float:
        """The full Indian fee for one delivery fill, in rupees.

        *side* is the engine's ``"buy"``/``"sell"``. A fund that somehow shorted
        would still be charged as a buy on the cover and a sell on the open,
        which is what a delivery broker does.
        """
        breakdown = compute_charges(
            side="BUY" if side == "buy" else "SELL",
            product=DELIVERY_PRODUCT,
            quantity=quantity,
            price_paise=_price_paise(price),
            schedule=self._rate_schedule,
        )
        return breakdown.total_paise / _PAISE_PER_RUPEE


def indian_delivery_costs(
    *,
    half_spread_bps: float = 5.0,
    min_trade_pct: float = 0.005,
    schedule: ChargeSchedule | None = None,
) -> IndianEquityCosts:
    """The cost model for an NSE delivery mandate.

    *half_spread_bps* is the only slippage assumption: the touch this app cannot
    see over daily closes. *min_trade_pct* is upstream's no-trade band (an add
    smaller than this share of equity waits). *schedule* lets a test pin exact
    rates; production uses `ChargeSchedule.indian_equities()`.
    """
    costs = IndianEquityCosts(
        spread_bps=half_spread_bps,
        commission_per_share=0.0,
        impact_coefficient=0.0,
        min_trade_pct=min_trade_pct,
    )
    if schedule is not None:
        costs._rate_schedule = schedule
    return costs


__all__ = [
    "DELIVERY_PRODUCT",
    "IndianEquityCosts",
    "indian_delivery_costs",
]
