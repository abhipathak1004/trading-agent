"""Code-owned MIS end-of-day square-off for paper accounts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from bq.core import CurrencyPaise, Quantity, SystemClock, paise_from_price
from bq.data.symbols import resolve as resolve_symbol
from bq.data.vendors import BarInterval, VendorRouter
from bq.paper_accounts import PaperAccountStatus, PaperAccountStore
from bq.paper_trading import (
    PaperOrderType,
    PaperProduct,
    PaperSide,
    PaperTradingError,
    PaperTradingStore,
)


class SquareOffError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SquareOffResult:
    examined: int
    squared_off: int
    notes: tuple[str, ...]


class PaperSquareOff:
    def __init__(self, path: Path, clock: SystemClock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.accounts = PaperAccountStore(path, self.clock)
        self.trading = PaperTradingStore(path, self.clock)

    def square_off(
        self, router: VendorRouter, *, as_of: datetime, account_id: str | None = None
    ) -> SquareOffResult:
        if as_of.utcoffset() is None:
            raise SquareOffError("as_of must be timezone-aware.")
        accounts = (
            (self.accounts.get(account_id),)
            if account_id is not None
            else self.accounts.list_accounts()
        )
        examined = 0
        squared = 0
        notes: list[str] = []
        for account in accounts:
            if account.status is not PaperAccountStatus.ACTIVE:
                continue
            positions = self.trading.product_positions(account.account_id)
            for position in positions:
                if position.product is not PaperProduct.MIS or position.quantity <= 0:
                    continue
                examined += 1
                client_order_id = (
                    f"square-off-{as_of.date().isoformat()}-"
                    f"{position.instrument_id.replace(':', '-')}"
                )
                try:
                    existing = self.trading.order_for_client_id(
                        account.account_id, client_order_id
                    )
                    if existing is not None:
                        continue
                    symbol = resolve_symbol(position.instrument_id)
                    result = router.bars(
                        symbol,
                        interval=BarInterval.DAY_1,
                        start=as_of - timedelta(days=7),
                        end=as_of,
                    )
                    bars = tuple(bar for bar in result.data if bar.event_at <= as_of)
                    if not bars:
                        raise SquareOffError("No bar for the square-off price.")
                    price = paise_from_price(bars[-1].close)
                    order = self.trading.place_order(
                        account_id=account.account_id,
                        instrument_id=position.instrument_id,
                        client_order_id=client_order_id,
                        side=PaperSide.SELL,
                        order_type=PaperOrderType.LIMIT,
                        quantity=Quantity(position.quantity),
                        limit_price_paise=CurrencyPaise(price.value),
                        product=PaperProduct.MIS,
                    )
                    self.trading.fill(
                        order.order_id,
                        quantity=Quantity(position.quantity),
                        price_paise=price,
                        idempotency_key=f"square-off-fill-{as_of.date().isoformat()}",
                    )
                    squared += 1
                except (PaperTradingError, ValueError, SquareOffError) as exc:
                    notes.append(
                        f"{account.account_id}/{position.instrument_id}: {exc}"
                    )
        return SquareOffResult(
            examined=examined, squared_off=squared, notes=tuple(notes)
        )


__all__ = ["PaperSquareOff", "SquareOffError", "SquareOffResult"]
