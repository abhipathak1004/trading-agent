import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from re import compile as regex

from bq.core import (
    Clock,
    CurrencyPaise,
    Quantity,
    SystemClock,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    parse_timestamp as core_parse_timestamp,
)
from bq.core import (
    utc as core_utc,
)
from bq.infra.audit import append_event
from bq.paper_accounts import LEDGER_TOTALS_SQL, PaperAccountStatus, PaperAccountStore


def _utc(value: datetime) -> datetime:
    try:
        return core_utc(value)
    except ValueError as exc:
        raise PaperTradingError("Paper timestamps must be timezone-aware.") from exc


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return core_parse_timestamp(value)


IDENTIFIER = regex(r"[A-Za-z0-9_-][A-Za-z0-9._:-]{0,127}")


class PaperSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PaperOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class PaperOrderStatus(StrEnum):
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


class PaperTradingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PaperOrder:
    order_id: str
    account_id: str
    instrument_id: str
    client_order_id: str
    side: PaperSide
    order_type: PaperOrderType
    quantity: int
    limit_price_paise: int | None
    status: PaperOrderStatus
    filled_quantity: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: str
    order_id: str
    quantity: int
    price_paise: int
    filled_at: datetime
    idempotency_key: str
    realized_pnl_paise: int | None = None


@dataclass(frozen=True, slots=True)
class PaperPosition:
    account_id: str
    instrument_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class PaperReconciliation:
    account_id: str
    order_count: int
    fill_count: int
    ledger_debit_paise: int
    ledger_credit_paise: int
    position_count: int


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise PaperTradingError(f"Invalid {label}.")


class PaperTradingStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.accounts = PaperAccountStore(path, clock)

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=True)

    def place_order(
        self,
        *,
        account_id: str,
        instrument_id: str,
        client_order_id: str,
        side: PaperSide,
        order_type: PaperOrderType,
        quantity: Quantity,
        limit_price_paise: CurrencyPaise | None = None,
    ) -> PaperOrder:
        _validate_identifier(account_id, "paper account identifier")
        _validate_identifier(instrument_id, "instrument identifier")
        _validate_identifier(client_order_id, "client order identifier")
        if not isinstance(side, PaperSide) or not isinstance(
            order_type, PaperOrderType
        ):
            raise PaperTradingError("Invalid paper order enums.")
        if not isinstance(quantity, Quantity) or quantity.value <= 0:
            raise PaperTradingError("Order quantity must be positive.")
        if order_type is PaperOrderType.LIMIT:
            if (
                not isinstance(limit_price_paise, CurrencyPaise)
                or limit_price_paise.value <= 0
            ):
                raise PaperTradingError("Limit orders require a positive limit price.")
            limit_value = limit_price_paise.value
        else:
            if limit_price_paise is not None:
                raise PaperTradingError("Market orders cannot have a limit price.")
            limit_value = None

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                account = self.accounts._get(connection, account_id)
                if account.status is not PaperAccountStatus.ACTIVE:
                    raise PaperTradingError("Orders require an active paper account.")
                existing = connection.execute(
                    "SELECT order_id FROM paper_orders "
                    "WHERE account_id = ? AND client_order_id = ?",
                    (account_id, client_order_id),
                ).fetchone()
                if existing is not None:
                    return self._get_order(connection, str(existing[0]))
                if side is PaperSide.SELL:
                    position = connection.execute(
                        "SELECT quantity FROM paper_positions "
                        "WHERE account_id = ? AND instrument_id = ?",
                        (account_id, instrument_id),
                    ).fetchone()
                    held = int(position[0]) if position is not None else 0
                    if held < quantity.value:
                        raise PaperTradingError("Sell quantity exceeds held position.")
                created_at = _utc(self.clock.now())
                order_id = secrets.token_urlsafe(18)
                connection.execute(
                    "INSERT INTO paper_orders "
                    "(order_id, account_id, instrument_id, client_order_id, side, "
                    "order_type, quantity, limit_price_paise, status, "
                    "filled_quantity, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)",
                    (
                        order_id,
                        account_id,
                        instrument_id,
                        client_order_id,
                        side.value,
                        order_type.value,
                        quantity.value,
                        limit_value,
                        _timestamp(created_at),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=created_at,
                    actor="owner",
                    action="paper.order.placed",
                    object_type="paper_order",
                    object_id=order_id,
                    payload={
                        "account_id": account_id,
                        "instrument_id": instrument_id,
                        "client_order_id": client_order_id,
                        "side": side.value,
                        "quantity": str(quantity.value),
                    },
                )
                return self._get_order(connection, order_id)

    def _fills_for_cost(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        instrument_id: str,
    ) -> list[tuple[str, str, int, int]]:
        rows = connection.execute(
            "SELECT f.fill_id, o.side, f.quantity, f.price_paise "
            "FROM paper_fills f JOIN paper_orders o ON o.order_id = f.order_id "
            "WHERE o.account_id = ? AND o.instrument_id = ? "
            "ORDER BY f.filled_at, f.rowid",
            (account_id, instrument_id),
        ).fetchall()
        return [(str(row[0]), str(row[1]), int(row[2]), int(row[3])) for row in rows]

    @staticmethod
    def _basis(rows: list[tuple[str, str, int, int]]) -> tuple[int, int]:
        held = 0
        cost = 0
        for _fill_id, side, quantity, price_paise in rows:
            if side == "BUY":
                held += quantity
                cost += quantity * price_paise
                continue
            if quantity > held:
                raise PaperTradingError("Existing sell fills have no cost basis.")
            if quantity == held:
                held = 0
                cost = 0
            else:
                removed = cost * quantity // held
                cost -= removed
                held -= quantity
        return held, cost

    @staticmethod
    def _realized_for_sell(
        held: int, cost: int, quantity: int, price_paise: int
    ) -> int:
        if quantity > held:
            raise PaperTradingError("Sell fill has no cost basis.")
        if quantity == held:
            return quantity * price_paise - cost
        removed = cost * quantity // held
        return quantity * price_paise - removed

    @classmethod
    def _realized_for_fill(
        cls,
        rows: list[tuple[str, str, int, int]],
        *,
        target_fill_id: str,
    ) -> int | None:
        held = 0
        cost = 0
        realized: int | None = None
        for fill_id, side, quantity, price_paise in rows:
            if side == "BUY":
                held += quantity
                cost += quantity * price_paise
                continue
            if quantity > held:
                raise PaperTradingError("Existing sell fills have no cost basis.")
            proceeds = quantity * price_paise
            if quantity == held:
                realized = proceeds - cost
                held = 0
                cost = 0
            else:
                removed = cost * quantity // held
                realized = proceeds - removed
                cost -= removed
                held -= quantity
            if fill_id == target_fill_id:
                return realized
        return None

    def fill(
        self,
        order_id: str,
        *,
        quantity: Quantity,
        price_paise: CurrencyPaise,
        idempotency_key: str,
    ) -> tuple[PaperOrder, PaperFill]:
        _validate_identifier(order_id, "paper order identifier")
        _validate_identifier(idempotency_key, "fill idempotency key")
        if not isinstance(quantity, Quantity) or quantity.value <= 0:
            raise PaperTradingError("Fill quantity must be positive.")
        if not isinstance(price_paise, CurrencyPaise) or price_paise.value <= 0:
            raise PaperTradingError("Fill price must be positive.")

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                order = self._get_order(connection, order_id)
                if order.status is PaperOrderStatus.CANCELLED:
                    raise PaperTradingError("Cannot fill a cancelled order.")
                existing = connection.execute(
                    "SELECT fill_id, quantity, price_paise, filled_at, idempotency_key "
                    "FROM paper_fills WHERE order_id = ? AND idempotency_key = ?",
                    (order_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    existing_id = str(existing[0])
                    duplicate_realized = None
                    if order.side is PaperSide.SELL:
                        duplicate_realized = self._realized_for_fill(
                            self._fills_for_cost(
                                connection, order.account_id, order.instrument_id
                            ),
                            target_fill_id=existing_id,
                        )
                    fill = PaperFill(
                        fill_id=existing_id,
                        order_id=order_id,
                        quantity=int(existing[1]),
                        price_paise=int(existing[2]),
                        filled_at=_parse_timestamp(str(existing[3])),
                        idempotency_key=str(existing[4]),
                        realized_pnl_paise=duplicate_realized,
                    )
                    return self._get_order(connection, order_id), fill

                remaining = order.quantity - order.filled_quantity
                if quantity.value > remaining:
                    raise PaperTradingError("Fill quantity exceeds remaining quantity.")
                if (
                    order.order_type is PaperOrderType.LIMIT
                    and order.limit_price_paise is not None
                    and (
                        (
                            order.side is PaperSide.BUY
                            and price_paise.value > order.limit_price_paise
                        )
                        or (
                            order.side is PaperSide.SELL
                            and price_paise.value < order.limit_price_paise
                        )
                    )
                ):
                    raise PaperTradingError("Fill violates the limit price.")

                realized: int | None = None
                if order.side is PaperSide.SELL:
                    held, cost = self._basis(
                        self._fills_for_cost(
                            connection, order.account_id, order.instrument_id
                        )
                    )
                    realized = self._realized_for_sell(
                        held, cost, quantity.value, price_paise.value
                    )

                filled_at = _utc(self.clock.now())
                fill_id = secrets.token_urlsafe(18)
                connection.execute(
                    "INSERT INTO paper_fills "
                    "(fill_id, order_id, quantity, price_paise, filled_at, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        fill_id,
                        order_id,
                        quantity.value,
                        price_paise.value,
                        _timestamp(filled_at),
                        idempotency_key,
                    ),
                )
                filled_quantity = order.filled_quantity + quantity.value
                status = (
                    PaperOrderStatus.FILLED
                    if filled_quantity == order.quantity
                    else PaperOrderStatus.PARTIALLY_FILLED
                )
                connection.execute(
                    "UPDATE paper_orders SET filled_quantity = ?, status = ? "
                    "WHERE order_id = ?",
                    (filled_quantity, status.value, order_id),
                )

                position_row = connection.execute(
                    "SELECT quantity FROM paper_positions "
                    "WHERE account_id = ? AND instrument_id = ?",
                    (order.account_id, order.instrument_id),
                ).fetchone()
                position_quantity = (
                    int(position_row[0]) if position_row is not None else 0
                )
                position_quantity += (
                    quantity.value if order.side is PaperSide.BUY else -quantity.value
                )
                if position_quantity < 0:
                    raise PaperTradingError("Fill would create a short stock position.")
                if position_row is None:
                    connection.execute(
                        "INSERT INTO paper_positions "
                        "(account_id, instrument_id, quantity, revision) "
                        "VALUES (?, ?, ?, 1)",
                        (order.account_id, order.instrument_id, position_quantity),
                    )
                else:
                    connection.execute(
                        "UPDATE paper_positions "
                        "SET quantity = ?, revision = revision + 1 "
                        "WHERE account_id = ? AND instrument_id = ?",
                        (position_quantity, order.account_id, order.instrument_id),
                    )

                notional = quantity.value * price_paise.value
                kind = "DEBIT" if order.side is PaperSide.BUY else "CREDIT"
                balance = self.accounts.balance(connection, order.account_id)
                if kind == "DEBIT" and notional > balance.balance_paise:
                    raise PaperTradingError("Fill exceeds available paper cash.")
                entry_id = secrets.token_urlsafe(18)
                connection.execute(
                    "INSERT INTO paper_ledger_entries "
                    "(entry_id, account_id, kind, amount_paise, occurred_at, "
                    "reference_type, reference_id) "
                    "VALUES (?, ?, ?, ?, ?, 'PAPER_FILL', ?)",
                    (
                        entry_id,
                        order.account_id,
                        kind,
                        notional,
                        _timestamp(filled_at),
                        fill_id,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=filled_at,
                    actor="paper_engine",
                    action="paper.fill.applied",
                    object_type="paper_fill",
                    object_id=fill_id,
                    payload={
                        "order_id": order_id,
                        "quantity": str(quantity.value),
                        "price_paise": str(price_paise.value),
                        "idempotency_key": idempotency_key,
                    },
                )
                return self._get_order(connection, order_id), PaperFill(
                    fill_id=fill_id,
                    order_id=order_id,
                    quantity=quantity.value,
                    price_paise=price_paise.value,
                    filled_at=filled_at,
                    idempotency_key=idempotency_key,
                    realized_pnl_paise=realized,
                )

    def cancel(self, order_id: str) -> PaperOrder:
        _validate_identifier(order_id, "paper order identifier")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                order = self._get_order(connection, order_id)
                if order.status in (
                    PaperOrderStatus.FILLED,
                    PaperOrderStatus.CANCELLED,
                ):
                    raise PaperTradingError(
                        "Only pending or partial orders can be cancelled."
                    )
                now = _utc(self.clock.now())
                connection.execute(
                    "UPDATE paper_orders SET status = 'CANCELLED' WHERE order_id = ?",
                    (order_id,),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="paper.order.cancelled",
                    object_type="paper_order",
                    object_id=order_id,
                    payload={"account_id": order.account_id},
                )
                return self._get_order(connection, order_id)

    def get_order(self, order_id: str) -> PaperOrder:
        _validate_identifier(order_id, "paper order identifier")
        with closing(self._connect()) as connection:
            return self._get_order(connection, order_id)

    def order_for_client_id(
        self, account_id: str, client_order_id: str
    ) -> PaperOrder | None:
        _validate_identifier(account_id, "paper account identifier")
        _validate_identifier(client_order_id, "client order identifier")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT order_id FROM paper_orders "
                "WHERE account_id = ? AND client_order_id = ?",
                (account_id, client_order_id),
            ).fetchone()
            if row is None:
                return None
            return self._get_order(connection, str(row[0]))

    def positions(self, account_id: str) -> tuple[PaperPosition, ...]:
        _validate_identifier(account_id, "paper account identifier")
        with closing(self._connect()) as connection:
            self.accounts._get(connection, account_id)
            rows = connection.execute(
                "SELECT account_id, instrument_id, quantity FROM paper_positions "
                "WHERE account_id = ? AND quantity > 0 ORDER BY instrument_id",
                (account_id,),
            ).fetchall()
        return tuple(
            PaperPosition(
                account_id=str(row[0]),
                instrument_id=str(row[1]),
                quantity=int(row[2]),
            )
            for row in rows
        )

    def recent_orders(
        self, account_id: str, *, limit: int = 20
    ) -> tuple[PaperOrder, ...]:
        _validate_identifier(account_id, "paper account identifier")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise PaperTradingError("Order limit must be between 1 and 100.")
        with closing(self._connect()) as connection:
            self.accounts._get(connection, account_id)
            rows = connection.execute(
                "SELECT order_id FROM paper_orders WHERE account_id = ? "
                "ORDER BY created_at DESC, order_id DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
            return tuple(self._get_order(connection, str(row[0])) for row in rows)

    def reconcile(self, account_id: str) -> PaperReconciliation:
        _validate_identifier(account_id, "paper account identifier")
        with closing(self._connect()) as connection:
            account = self.accounts._get(connection, account_id)
            orders = connection.execute(
                "SELECT order_id, quantity, filled_quantity, status "
                "FROM paper_orders WHERE account_id = ?",
                (account.account_id,),
            ).fetchall()
            for order_id, quantity, filled_quantity, status in orders:
                if int(filled_quantity) > int(quantity) or (
                    status == "FILLED" and int(filled_quantity) != int(quantity)
                ):
                    raise PaperTradingError(
                        f"Paper reconciliation mismatch for order {order_id}."
                    )
            fills = connection.execute(
                "SELECT order_id, quantity "
                "FROM paper_fills "
                "WHERE order_id IN "
                "(SELECT order_id FROM paper_orders WHERE account_id = ?)",
                (account.account_id,),
            ).fetchall()
            fill_totals: dict[str, int] = {}
            for order_id, quantity in fills:
                fill_totals[str(order_id)] = fill_totals.get(str(order_id), 0) + int(
                    quantity
                )
            for order_id, _quantity, filled_quantity, _status in orders:
                if fill_totals.get(str(order_id), 0) != int(filled_quantity):
                    raise PaperTradingError(
                        f"Fill totals mismatch for order {order_id}."
                    )
            ledger = connection.execute(
                LEDGER_TOTALS_SQL, (account.account_id,)
            ).fetchone()
            position_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM paper_positions "
                    "WHERE account_id = ? AND quantity > 0",
                    (account.account_id,),
                ).fetchone()[0]
            )
            return PaperReconciliation(
                account_id=account.account_id,
                order_count=len(orders),
                fill_count=len(fills),
                ledger_debit_paise=int(ledger[0]),
                ledger_credit_paise=int(ledger[1]),
                position_count=position_count,
            )

    def _get_order(self, connection: sqlite3.Connection, order_id: str) -> PaperOrder:
        row = connection.execute(
            "SELECT order_id, account_id, instrument_id, client_order_id, side, "
            "order_type, quantity, limit_price_paise, status, filled_quantity, created_at "
            "FROM paper_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise PaperTradingError("Paper order not found.")
        return PaperOrder(
            order_id=str(row[0]),
            account_id=str(row[1]),
            instrument_id=str(row[2]),
            client_order_id=str(row[3]),
            side=PaperSide(str(row[4])),
            order_type=PaperOrderType(str(row[5])),
            quantity=int(row[6]),
            limit_price_paise=int(row[7]) if row[7] is not None else None,
            status=PaperOrderStatus(str(row[8])),
            filled_quantity=int(row[9]),
            created_at=_parse_timestamp(str(row[10])),
        )
