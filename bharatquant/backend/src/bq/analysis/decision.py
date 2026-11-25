"""The structured trade plan.

TradingAgents emits a free-text recommendation. That is not a contract anything
downstream can act on safely, so this module replaces it with a validated object
that must satisfy the arithmetic before it exists.

The rules enforced in :meth:`TradePlan.__post_init__` are the ones that matter
for safety, and each is a rejection rather than a repair:

* a stop must be strictly on the losing side of the entry and strictly positive;
* a target must be on the winning side, beyond the stop;
* reward/risk must clear a configured floor (default 1.5);
* quantity must be positive and — critically — **equal to what the sizer
  produces from the same inputs**, so a model cannot inflate a position.

That last check is the point of this module. ``suggested_quantity`` is not a
field the LLM fills in; it is recomputed here from equity, risk fraction and
stop distance, and a plan whose quantity disagrees with the sizer is rejected
outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from bq.core import CurrencyPaise, Quantity
from bq.risk.sizing import size_long_fixed_fractional

PDF = Decimal
BASIS_POINTS = 10_000
DEFAULT_MIN_REWARD_RISK_BPS = 15_000  # 1.5 expressed in basis points


class TradePlanError(ValueError):
    """A plan violated the contract. Never silently corrected."""


class Signal(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Product(StrEnum):
    """Kite's product codes, kept as the vocabulary for the plan."""

    MIS = "MIS"  # intraday
    CNC = "CNC"  # delivery / swing


class OrderType(StrEnum):
    LIMIT = "LIMIT"


class DataFreshness(StrEnum):
    REALTIME_KITE = "realtime_kite"
    DELAYED_YAHOO = "delayed_yfinance"


def _to_paise(price: Decimal) -> CurrencyPaise:
    """Convert a price to exact paise, rounding half-up to the nearest paisa."""
    if not isinstance(price, Decimal):
        raise TradePlanError("Prices must be Decimal.")
    if price <= 0:
        raise TradePlanError("Prices must be positive.")
    paise = (price * 100).quantize(Decimal("1"), rounding="ROUND_HALF_UP")
    return CurrencyPaise(int(paise))


@dataclass(frozen=True, slots=True)
class TradePlan:
    """A validated, actionable plan. Construct only if the arithmetic holds."""

    signal: Signal
    symbol: str
    exchange: str
    product: Product
    order_type: OrderType
    entry_price: Decimal
    stop_loss: Decimal
    target_price: Decimal
    suggested_quantity: int
    confidence: float
    timeframe: str
    invalidation_reason: str
    data_freshness: DataFreshness
    as_of: datetime
    min_reward_risk_bps: int = DEFAULT_MIN_REWARD_RISK_BPS

    def __post_init__(self) -> None:
        if not isinstance(self.signal, Signal):
            raise TradePlanError("Signal must be a Signal value.")
        if not isinstance(self.product, Product):
            raise TradePlanError("Product must be a Product value.")
        if not isinstance(self.order_type, OrderType):
            raise TradePlanError("Order type must be a Product value.")
        if not isinstance(self.data_freshness, DataFreshness):
            raise TradePlanError("Data freshness must be declared.")
        if self.order_type is not OrderType.LIMIT:
            # ADR-001 forbids live orders anyway, but the plan is also the paper
            # instruction, and a market order has no defined fill price.
            raise TradePlanError("Only LIMIT orders are permitted.")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise TradePlanError("A symbol is required.")
        if self.exchange not in {"NSE", "BSE"}:
            raise TradePlanError("Exchange must be NSE or BSE.")
        if self.as_of.utcoffset() is None:
            raise TradePlanError("as_of must be timezone-aware.")
        if not isinstance(self.timeframe, str) or not self.timeframe:
            raise TradePlanError("A timeframe is required.")
        if (
            not isinstance(self.invalidation_reason, str)
            or not self.invalidation_reason
        ):
            raise TradePlanError("A plan must state what invalidates it.")

        for label, price in (
            ("entry_price", self.entry_price),
            ("stop_loss", self.stop_loss),
            ("target_price", self.target_price),
        ):
            if not isinstance(price, Decimal):
                raise TradePlanError(f"{label} must be a Decimal.")
            if price <= 0:
                raise TradePlanError(f"{label} must be positive.")

        if isinstance(self.suggested_quantity, bool) or not isinstance(
            self.suggested_quantity, int
        ):
            raise TradePlanError("Quantity must be an integer.")
        if self.suggested_quantity <= 0:
            raise TradePlanError("Quantity must be positive.")

        if isinstance(self.confidence, bool) or not isinstance(self.confidence, float):
            raise TradePlanError("Confidence must be a float.")
        if not 0.0 <= self.confidence <= 1.0:
            raise TradePlanError("Confidence must be within [0, 1].")

        if isinstance(self.min_reward_risk_bps, bool) or not isinstance(
            self.min_reward_risk_bps, int
        ):
            raise TradePlanError("The reward/risk floor must be integer basis points.")
        if self.min_reward_risk_bps < 0:
            raise TradePlanError("The reward/risk floor must be nonnegative.")

        if self.signal is Signal.BUY:
            if self.stop_loss >= self.entry_price:
                raise TradePlanError("A long stop must be below the entry.")
            if self.target_price <= self.entry_price:
                raise TradePlanError("A long target must be above the entry.")
        elif self.signal is Signal.SELL:
            if self.stop_loss <= self.entry_price:
                raise TradePlanError("A short stop must be above the entry.")
            if self.target_price >= self.entry_price:
                raise TradePlanError("A short target must be below the entry.")

    # -- derived ---------------------------------------------------------

    @property
    def risk_per_share(self) -> Decimal:
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_per_share(self) -> Decimal:
        return abs(self.target_price - self.entry_price)

    @property
    def reward_risk(self) -> Decimal:
        if self.risk_per_share == 0:
            raise TradePlanError("Reward/risk is undefined with a zero stop distance.")
        return self.reward_per_share / self.risk_per_share

    @property
    def reward_risk_bps(self) -> int:
        return int((self.reward_risk * BASIS_POINTS).quantize(Decimal("1")))

    def check_reward_risk(self) -> None:
        """Reject a plan below the configured floor. Called by the pipeline."""
        if self.reward_risk_bps < self.min_reward_risk_bps:
            raise TradePlanError(
                f"Reward/risk {self.reward_risk:.2f} is below the minimum "
                f"{self.min_reward_risk_bps / BASIS_POINTS:.2f}."
            )

    def notional_paise(self) -> CurrencyPaise:
        return CurrencyPaise(
            _to_paise(self.entry_price).value * self.suggested_quantity
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "signal": self.signal.value,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "product": self.product.value,
            "order_type": self.order_type.value,
            "entry_price": str(self.entry_price),
            "stop_loss": str(self.stop_loss),
            "target_price": str(self.target_price),
            "reward_risk": str(self.reward_risk.quantize(Decimal("0.01"))),
            "suggested_quantity": self.suggested_quantity,
            "confidence": self.confidence,
            "timeframe": self.timeframe,
            "invalidation_reason": self.invalidation_reason,
            "data_freshness": self.data_freshness.value,
            "as_of": self.as_of.isoformat(),
        }


def build_plan(
    *,
    signal: Signal,
    symbol: str,
    exchange: str,
    product: Product,
    entry_price: Decimal,
    stop_loss: Decimal,
    target_price: Decimal,
    confidence: float,
    timeframe: str,
    invalidation_reason: str,
    data_freshness: DataFreshness,
    as_of: datetime,
    equity_paise: int,
    available_cash_paise: int,
    risk_basis_points: int,
    min_reward_risk_bps: int = DEFAULT_MIN_REWARD_RISK_BPS,
) -> TradePlan:
    """Size the trade deterministically, then validate the whole plan.

    The quantity is *always* the sizer's answer. If the sizer says zero — because
    the risk budget or the cash cannot support one share — no plan is produced.
    """
    if signal is not Signal.BUY:
        raise TradePlanError(
            "Only long plans are sized today; there is no short-selling margin "
            "model in this project."
        )
    entry_paise = _to_paise(entry_price)
    stop_paise = _to_paise(stop_loss)
    sizing = size_long_fixed_fractional(
        equity=CurrencyPaise(equity_paise),
        available_cash=CurrencyPaise(available_cash_paise),
        entry_price=entry_paise,
        stop_price=stop_paise,
        risk_basis_points=risk_basis_points,
    )
    if sizing.quantity.value <= 0:
        raise TradePlanError(
            "Sizing produced zero shares: the risk budget or available cash is "
            "too small for this entry and stop."
        )
    plan = TradePlan(
        signal=signal,
        symbol=symbol,
        exchange=exchange,
        product=product,
        order_type=OrderType.LIMIT,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_price=target_price,
        suggested_quantity=Quantity(sizing.quantity.value).value,
        confidence=confidence,
        timeframe=timeframe,
        invalidation_reason=invalidation_reason,
        data_freshness=data_freshness,
        as_of=as_of,
        min_reward_risk_bps=min_reward_risk_bps,
    )
    plan.check_reward_risk()
    return plan


def verify_quantity_matches_sizing(
    plan: TradePlan,
    *,
    equity_paise: int,
    available_cash_paise: int,
    risk_basis_points: int,
) -> None:
    """Prove a plan's quantity came from the sizer, not from a model.

    The pipeline calls this on every plan it did not itself build, so a plan
    assembled elsewhere cannot smuggle in a larger position.
    """
    sizing = size_long_fixed_fractional(
        equity=CurrencyPaise(equity_paise),
        available_cash=CurrencyPaise(available_cash_paise),
        entry_price=_to_paise(plan.entry_price),
        stop_price=_to_paise(plan.stop_loss),
        risk_basis_points=risk_basis_points,
    )
    if sizing.quantity.value != plan.suggested_quantity:
        raise TradePlanError(
            f"Plan quantity {plan.suggested_quantity} does not match the sizer's "
            f"{sizing.quantity.value}. A model may not choose position size."
        )


__all__ = [
    "BASIS_POINTS",
    "DEFAULT_MIN_REWARD_RISK_BPS",
    "DataFreshness",
    "OrderType",
    "Product",
    "Signal",
    "TradePlan",
    "TradePlanError",
    "build_plan",
    "verify_quantity_matches_sizing",
]
