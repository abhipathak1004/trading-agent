"""Deterministic, code-owned Indian equity charge model for paper fills.

The rates are v1 estimates for research accounting, not a broker contract. Every
component is itemized so the owner can audit the gross-to-net bridge and update the
schedule without touching fill logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

MAX_QUANTITY = 10**9
MAX_PRICE_PAISE = 10**13
VENUES = frozenset({"NSE", "BSE"})
PRODUCT_DELIVERY = "CAS"
PRODUCT_INTRADAY = "MIS"


class ChargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChargeComponent:
    code: str
    amount_paise: int


@dataclass(frozen=True, slots=True)
class ChargeBreakdown:
    components: tuple[ChargeComponent, ...]
    total_paise: int

    def amount(self, code: str) -> int:
        return next(
            (item.amount_paise for item in self.components if item.code == code), 0
        )


@dataclass(frozen=True, slots=True)
class ChargeSchedule:
    stt_cas_buy: Decimal = Decimal("0.001")
    stt_cas_sell: Decimal = Decimal("0.001")
    stt_mis_sell: Decimal = Decimal("0.00025")
    exchange: Decimal = Decimal("0.0000297")
    sebi: Decimal = Decimal("0.000001")
    stamp_cas_buy: Decimal = Decimal("0.00015")
    stamp_mis_buy: Decimal = Decimal("0.00003")
    gst: Decimal = Decimal("0.18")
    # --- Components this model used to omit (ADR-054) ---------------------
    # All default to zero so an existing backtest or paper run reproduces
    # exactly. A non-zero brokerage is the owner's call: guessing a per-order
    # rate would silently reprice every historical run.
    brokerage_bps: Decimal = Decimal("0")
    brokerage_flat_paise: int = 0
    # 0 means "no cap". A real broker cap is per *order*, which is this call.
    brokerage_cap_paise: int = 0
    ipft: Decimal = Decimal("0")
    # Delivery DP charge is levied per sell, not per share.
    dp_sell_paise: int = 0
    # None means "same as `exchange`".
    exchange_bse: Decimal | None = None

    @classmethod
    def indian_equities(cls) -> ChargeSchedule:
        return cls()

    @classmethod
    def zero(cls) -> ChargeSchedule:
        zero = Decimal("0")
        return cls(
            stt_cas_buy=zero,
            stt_cas_sell=zero,
            stt_mis_sell=zero,
            exchange=zero,
            sebi=zero,
            stamp_cas_buy=zero,
            stamp_mis_buy=zero,
            gst=zero,
            brokerage_bps=zero,
            ipft=zero,
            exchange_bse=zero,
        )


def _rounded(amount_paise: int, rate: Decimal) -> int:
    return int(
        (Decimal(amount_paise) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def compute_charges(
    *,
    side: str,
    product: str,
    quantity: int,
    price_paise: int,
    schedule: ChargeSchedule | None = None,
    venue: str = "NSE",
) -> ChargeBreakdown:
    if side not in {"BUY", "SELL"}:
        raise ChargeError("Charge side must be BUY or SELL.")
    if product not in {"CAS", "MIS"}:
        raise ChargeError("Charge product must be CAS or MIS.")
    if venue not in VENUES:
        raise ChargeError("Charge venue must be NSE or BSE.")
    if (
        isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or not 1 <= quantity <= MAX_QUANTITY
    ):
        raise ChargeError("Charge quantity is outside its bound.")
    if (
        isinstance(price_paise, bool)
        or not isinstance(price_paise, int)
        or not 1 <= price_paise <= MAX_PRICE_PAISE
    ):
        raise ChargeError("Charge price is outside its bound.")
    rates = schedule or ChargeSchedule.indian_equities()
    if rates.brokerage_bps < 0 or rates.ipft < 0:
        raise ChargeError("Charge rates must not be negative.")
    notional = quantity * price_paise
    components: list[ChargeComponent] = []

    brokerage = _rounded(notional, rates.brokerage_bps / Decimal("10000"))
    brokerage += rates.brokerage_flat_paise
    if rates.brokerage_cap_paise > 0:
        brokerage = min(brokerage, rates.brokerage_cap_paise)
    if brokerage > 0:
        components.append(ChargeComponent("BROKERAGE", brokerage))

    if side == "BUY" and product == "CAS":
        components.append(ChargeComponent("STT", _rounded(notional, rates.stt_cas_buy)))
    elif side == "SELL" and product == "CAS":
        components.append(
            ChargeComponent("STT", _rounded(notional, rates.stt_cas_sell))
        )
    elif side == "SELL":
        components.append(
            ChargeComponent("STT", _rounded(notional, rates.stt_mis_sell))
        )

    exchange_rate = rates.exchange
    if venue == "BSE" and rates.exchange_bse is not None:
        exchange_rate = rates.exchange_bse
    exchange = _rounded(notional, exchange_rate)
    sebi = _rounded(notional, rates.sebi)
    ipft = _rounded(notional, rates.ipft)
    components.append(ChargeComponent("EXCHANGE", exchange))
    components.append(ChargeComponent("SEBI", sebi))
    if ipft > 0:
        components.append(ChargeComponent("IPFT", ipft))
    if side == "SELL" and product == "CAS" and rates.dp_sell_paise > 0:
        components.append(ChargeComponent("DP", rates.dp_sell_paise))
    if side == "BUY":
        stamp_rate = rates.stamp_cas_buy if product == "CAS" else rates.stamp_mis_buy
        components.append(ChargeComponent("STAMP", _rounded(notional, stamp_rate)))
    # GST is levied on brokerage + exchange + SEBI + IPFT. The previous model
    # omitted brokerage from the base because it had no brokerage component.
    gst = _rounded(brokerage + exchange + sebi + ipft, rates.gst)
    components.append(ChargeComponent("GST", gst))
    nonzero = tuple(item for item in components if item.amount_paise > 0)
    return ChargeBreakdown(
        components=nonzero,
        total_paise=sum(item.amount_paise for item in nonzero),
    )


def product_for_timeframe(timeframe: str) -> str:
    """Map a strategy timeframe to the product its fills settle under."""
    return PRODUCT_INTRADAY if timeframe == "intraday" else PRODUCT_DELIVERY


@dataclass(frozen=True, slots=True)
class IndiaCostModel:
    """A product- and venue-aware wrapper around one charge schedule.

    This is what a run records as its ``theta``: the rates actually applied, the
    venue and the product rule, so a replay can be explained later.
    """

    schedule: ChargeSchedule = field(default_factory=ChargeSchedule.indian_equities)
    venue: str = "NSE"

    def product_for(self, timeframe: str) -> str:
        return product_for_timeframe(timeframe)

    def charges(
        self,
        *,
        side: str,
        timeframe: str,
        quantity: int,
        price_paise: int,
    ) -> ChargeBreakdown:
        return compute_charges(
            side=side,
            product=self.product_for(timeframe),
            quantity=quantity,
            price_paise=price_paise,
            schedule=self.schedule,
            venue=self.venue,
        )

    def theta(self) -> dict[str, object]:
        """The cost assumptions, in a form that round-trips through JSON."""
        rates = self.schedule
        return {
            "venue": self.venue,
            "brokerage_bps": str(rates.brokerage_bps),
            "brokerage_flat_paise": rates.brokerage_flat_paise,
            "brokerage_cap_paise": rates.brokerage_cap_paise,
            "exchange": str(rates.exchange),
            "exchange_bse": (
                None if rates.exchange_bse is None else str(rates.exchange_bse)
            ),
            "sebi": str(rates.sebi),
            "ipft": str(rates.ipft),
            "dp_sell_paise": rates.dp_sell_paise,
            "stt_cas_buy": str(rates.stt_cas_buy),
            "stt_cas_sell": str(rates.stt_cas_sell),
            "stt_mis_sell": str(rates.stt_mis_sell),
            "stamp_cas_buy": str(rates.stamp_cas_buy),
            "stamp_mis_buy": str(rates.stamp_mis_buy),
            "gst": str(rates.gst),
        }


__all__ = [
    "ChargeBreakdown",
    "ChargeComponent",
    "ChargeError",
    "ChargeSchedule",
    "IndiaCostModel",
    "PRODUCT_DELIVERY",
    "PRODUCT_INTRADAY",
    "VENUES",
    "compute_charges",
    "product_for_timeframe",
]
