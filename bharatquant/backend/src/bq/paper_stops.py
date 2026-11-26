"""Server-side paper stop orders, evaluated from daily bars.

A stop is a position exit, never an entry. The worker evaluates active stops and
creates a paper SELL order at the first touched stop or target. The stop row is
the idempotency boundary: it may trigger once.
"""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from bq.core import Clock, CurrencyPaise, Quantity, SystemClock, open_connection
from bq.core import parse_timestamp as core_parse_timestamp
from bq.core import utc as core_utc
from bq.data.symbols import resolve as resolve_symbol
from bq.data.vendors import BarInterval, VendorRouter
from bq.infra.audit import append_event
from bq.paper_trading import (
    PaperOrderType,
    PaperProduct,
    PaperSide,
    PaperTradingError,
    PaperTradingStore,
)


class PaperStopError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PaperStop:
    stop_id: str
    account_id: str
    instrument_id: str
    product: PaperProduct
    quantity: int
    stop_price_paise: int
    target_price_paise: int | None
    status: str
    trigger_order_id: str | None
    trigger_kind: str | None
    created_at: datetime
    triggered_at: datetime | None


@dataclass(frozen=True, slots=True)
class StopEvaluation:
    examined: int
    triggered: int
    notes: tuple[str, ...]


def _timestamp(value: datetime) -> str:
    return core_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


class PaperStopStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.trading = PaperTradingStore(path, clock)

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def create(
        self,
        *,
        account_id: str,
        instrument_id: str,
        quantity: Quantity,
        stop_price_paise: CurrencyPaise,
        target_price_paise: CurrencyPaise | None = None,
        product: PaperProduct = PaperProduct.CAS,
    ) -> PaperStop:
        if not isinstance(quantity, Quantity) or quantity.value <= 0:
            raise PaperStopError("Stop quantity must be positive.")
        if (
            not isinstance(stop_price_paise, CurrencyPaise)
            or stop_price_paise.value <= 0
        ):
            raise PaperStopError("Stop price must be positive.")
        if target_price_paise is not None and (
            not isinstance(target_price_paise, CurrencyPaise)
            or target_price_paise.value <= stop_price_paise.value
        ):
            raise PaperStopError("Target must be above the stop price.")
        if not isinstance(product, PaperProduct):
            raise PaperStopError("Invalid product.")
        positions = self.trading.product_positions(account_id)
        held = next(
            (
                item.quantity
                for item in positions
                if item.instrument_id == instrument_id and item.product is product
            ),
            0,
        )
        if held < quantity.value:
            raise PaperStopError("Stop quantity exceeds the held product position.")
        now = core_utc(self.clock.now())
        stop_id = secrets.token_urlsafe(18)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO paper_stops "
                    "(stop_id, account_id, instrument_id, product, quantity, "
                    "stop_price_paise, target_price_paise, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)",
                    (
                        stop_id,
                        account_id,
                        instrument_id,
                        product.value,
                        quantity.value,
                        stop_price_paise.value,
                        target_price_paise.value if target_price_paise else None,
                        _timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="paper.stop.created",
                    object_type="paper_stop",
                    object_id=stop_id,
                    payload={
                        "account_id": account_id,
                        "instrument_id": instrument_id,
                        "quantity": str(quantity.value),
                    },
                )
        return self.get(stop_id)

    def get(self, stop_id: str) -> PaperStop:
        if not isinstance(stop_id, str) or not stop_id:
            raise PaperStopError("Invalid stop identifier.")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT stop_id, account_id, instrument_id, product, quantity, "
                "stop_price_paise, target_price_paise, status, trigger_order_id, "
                "trigger_kind, created_at, triggered_at FROM paper_stops "
                "WHERE stop_id = ?",
                (stop_id,),
            ).fetchone()
        if row is None:
            raise PaperStopError("Paper stop not found.")
        return self._row(row)

    def list(
        self, account_id: str, *, status: str | None = None
    ) -> tuple[PaperStop, ...]:
        if status is not None and status not in {"ACTIVE", "TRIGGERED", "CANCELLED"}:
            raise PaperStopError("Invalid stop status.")
        with closing(self._connect()) as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT stop_id, account_id, instrument_id, product, quantity, "
                    "stop_price_paise, target_price_paise, status, trigger_order_id, "
                    "trigger_kind, created_at, triggered_at FROM paper_stops "
                    "WHERE account_id = ? ORDER BY created_at DESC, stop_id",
                    (account_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT stop_id, account_id, instrument_id, product, quantity, "
                    "stop_price_paise, target_price_paise, status, trigger_order_id, "
                    "trigger_kind, created_at, triggered_at FROM paper_stops "
                    "WHERE account_id = ? AND status = ? "
                    "ORDER BY created_at DESC, stop_id",
                    (account_id, status),
                ).fetchall()
        return tuple(self._row(row) for row in rows)

    def cancel(self, stop_id: str) -> PaperStop:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                row = connection.execute(
                    "SELECT status FROM paper_stops WHERE stop_id = ?", (stop_id,)
                ).fetchone()
                if row is None:
                    raise PaperStopError("Paper stop not found.")
                if str(row[0]) != "ACTIVE":
                    raise PaperStopError("Only an active stop can be cancelled.")
                now = core_utc(self.clock.now())
                connection.execute(
                    "UPDATE paper_stops SET status = 'CANCELLED' WHERE stop_id = ?",
                    (stop_id,),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="paper.stop.cancelled",
                    object_type="paper_stop",
                    object_id=stop_id,
                    payload={},
                )
        return self.get(stop_id)

    def evaluate(self, router: VendorRouter, *, as_of: datetime) -> StopEvaluation:
        if as_of.utcoffset() is None:
            raise PaperStopError("as_of must be timezone-aware.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT stop_id, account_id, instrument_id, product, quantity, "
                "stop_price_paise, target_price_paise, status, trigger_order_id, "
                "trigger_kind, created_at, triggered_at FROM paper_stops "
                "WHERE status = 'ACTIVE' ORDER BY created_at, stop_id"
            ).fetchall()
        stops = tuple(self._row(row) for row in rows)
        triggered = 0
        notes: list[str] = []
        for stop in stops:
            try:
                if self._evaluate_one(stop, router, as_of):
                    triggered += 1
            except Exception as exc:  # noqa: BLE001 - isolate one bad stop
                notes.append(f"{stop.stop_id}: {type(exc).__name__}: {exc}")
        return StopEvaluation(
            examined=len(stops), triggered=triggered, notes=tuple(notes)
        )

    def _evaluate_one(
        self, stop: PaperStop, router: VendorRouter, as_of: datetime
    ) -> bool:
        symbol = resolve_symbol(stop.instrument_id)
        result = router.bars(
            symbol,
            interval=BarInterval.DAY_1,
            start=stop.created_at - timedelta(days=1),
            end=as_of,
        )
        bars = tuple(
            bar for bar in result.data if stop.created_at <= bar.event_at <= as_of
        )
        if not bars:
            return False
        trigger_kind: str | None = None
        trigger_price = 0
        for bar in bars:
            if int(bar.low * 100) <= stop.stop_price_paise:
                trigger_kind = "STOP"
                trigger_price = stop.stop_price_paise
                break
            if (
                stop.target_price_paise is not None
                and int(bar.high * 100) >= stop.target_price_paise
            ):
                trigger_kind = "TARGET"
                trigger_price = stop.target_price_paise
                break
        if trigger_kind is None:
            return False

        client_order_id = f"stop-{stop.stop_id}"
        existing = self.trading.order_for_client_id(stop.account_id, client_order_id)
        if existing is not None:
            order = existing
        else:
            try:
                order = self.trading.place_order(
                    account_id=stop.account_id,
                    instrument_id=stop.instrument_id,
                    client_order_id=client_order_id,
                    side=PaperSide.SELL,
                    order_type=PaperOrderType.LIMIT,
                    quantity=Quantity(stop.quantity),
                    limit_price_paise=CurrencyPaise(trigger_price),
                    product=stop.product,
                    approval_reason="Server-side stop/target exit",
                )
            except PaperTradingError:
                raise
        try:
            self.trading.fill(
                order.order_id,
                quantity=Quantity(stop.quantity),
                price_paise=CurrencyPaise(trigger_price),
                idempotency_key=f"stop-fill-{stop.stop_id}",
            )
        except PaperTradingError:
            # The exit order is durable and visible even if settlement or cash
            # blocks the fill. The owner can resolve it from the Trade panel.
            pass

        now = core_utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                updated = connection.execute(
                    "UPDATE paper_stops SET status = 'TRIGGERED', "
                    "trigger_order_id = ?, trigger_kind = ?, triggered_at = ? "
                    "WHERE stop_id = ? AND status = 'ACTIVE'",
                    (
                        order.order_id,
                        trigger_kind,
                        _timestamp(now),
                        stop.stop_id,
                    ),
                )
                if updated.rowcount != 1:
                    return False
                append_event(
                    connection,
                    occurred_at=now,
                    actor="paper_engine",
                    action="paper.stop.triggered",
                    object_type="paper_stop",
                    object_id=stop.stop_id,
                    payload={
                        "trigger_kind": trigger_kind,
                        "trigger_order_id": order.order_id,
                    },
                )
        return True

    @staticmethod
    def _row(row: tuple[object, ...]) -> PaperStop:
        return PaperStop(
            stop_id=str(row[0]),
            account_id=str(row[1]),
            instrument_id=str(row[2]),
            product=PaperProduct(str(row[3])),
            quantity=int(str(row[4])),
            stop_price_paise=int(str(row[5])),
            target_price_paise=int(str(row[6])) if row[6] is not None else None,
            status=str(row[7]),
            trigger_order_id=str(row[8]) if row[8] is not None else None,
            trigger_kind=str(row[9]) if row[9] is not None else None,
            created_at=core_parse_timestamp(str(row[10])),
            triggered_at=(
                core_parse_timestamp(str(row[11])) if row[11] is not None else None
            ),
        )


__all__ = [
    "PaperStop",
    "PaperStopError",
    "PaperStopStore",
    "StopEvaluation",
]
