"""Deterministic, code-owned Indian equity charge model for paper fills.

The rates are v1 estimates for research accounting, not a broker contract. Every
component is itemized so the owner can audit the gross-to-net bridge and update the
schedule without touching fill logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

MAX_QUANTITY = 10**9
MAX_PRICE_PAISE = 10**13


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
) -> ChargeBreakdown:
    if side not in {"BUY", "SELL"}:
        raise ChargeError("Charge side must be BUY or SELL.")
    if product not in {"CAS", "MIS"}:
        raise ChargeError("Charge product must be CAS or MIS.")
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
    notional = quantity * price_paise
    components: list[ChargeComponent] = []

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

    exchange = _rounded(notional, rates.exchange)
    sebi = _rounded(notional, rates.sebi)
    components.append(ChargeComponent("EXCHANGE", exchange))
    components.append(ChargeComponent("SEBI", sebi))
    if side == "BUY":
        stamp_rate = rates.stamp_cas_buy if product == "CAS" else rates.stamp_mis_buy
        components.append(ChargeComponent("STAMP", _rounded(notional, stamp_rate)))
    gst = _rounded(exchange + sebi, rates.gst)
    components.append(ChargeComponent("GST", gst))
    nonzero = tuple(item for item in components if item.amount_paise > 0)
    return ChargeBreakdown(
        components=nonzero,
        total_paise=sum(item.amount_paise for item in nonzero),
    )


__all__ = [
    "ChargeBreakdown",
    "ChargeComponent",
    "ChargeError",
    "ChargeSchedule",
    "compute_charges",
]
