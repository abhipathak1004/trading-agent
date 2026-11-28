import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from re import compile as regex
from zoneinfo import ZoneInfo

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
from bq.paper_charges import ChargeBreakdown, ChargeSchedule, compute_charges


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


class PaperProduct(StrEnum):
    CAS = "CAS"
    MIS = "MIS"


IST = ZoneInfo("Asia/Kolkata")


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
    product: PaperProduct = PaperProduct.MIS
    approval_status: str = "APPROVED"


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: str
    order_id: str
    quantity: int
    price_paise: int
    filled_at: datetime
    idempotency_key: str
    realized_pnl_paise: int | None = None
    gross_paise: int = 0
    charges_paise: int = 0
    net_pnl_paise: int | None = None


@dataclass(frozen=True, slots=True)
class PaperPosition:
    account_id: str
    instrument_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class ProductPosition:
    account_id: str
    instrument_id: str
    product: PaperProduct
    quantity: int


@dataclass(frozen=True, slots=True)
class PaperTaxLot:
    lot_id: str
    account_id: str
    instrument_id: str
    product: PaperProduct
    buy_fill_id: str
    quantity: int
    remaining_quantity: int
    cost_paise: int
    charges_paise: int
    acquired_at: datetime
    settles_on: date


@dataclass(frozen=True, slots=True)
class PaperApproval:
    approval_id: str
    order_id: str
    account_id: str
    status: str
    reason: str
    created_at: datetime
    decided_at: datetime | None
    decision_note: str | None


@dataclass(frozen=True, slots=True)
class PaperReconciliation:
    account_id: str
    order_count: int
    fill_count: int
    ledger_debit_paise: int
    ledger_credit_paise: int
    position_count: int
    charges_paise: int = 0


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise PaperTradingError(f"Invalid {label}.")


class PaperTradingStore:
    def __init__(
        self,
        path: Path,
        clock: Clock | None = None,
        *,
        charge_schedule: ChargeSchedule | None = None,
    ) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.charge_schedule = charge_schedule or ChargeSchedule.indian_equities()
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
        product: PaperProduct = PaperProduct.MIS,
        require_approval: bool = False,
        approval_reason: str = "Manual owner approval",
    ) -> PaperOrder:
        _validate_identifier(account_id, "paper account identifier")
        _validate_identifier(instrument_id, "instrument identifier")
        _validate_identifier(client_order_id, "client order identifier")
        if not isinstance(side, PaperSide) or not isinstance(
            order_type, PaperOrderType
        ):
            raise PaperTradingError("Invalid paper order enums.")
        if not isinstance(product, PaperProduct):
            raise PaperTradingError("Invalid paper product.")
        if not isinstance(require_approval, bool):
            raise PaperTradingError("Approval flag must be a boolean.")
        if not isinstance(approval_reason, str) or not 1 <= len(approval_reason) <= 512:
            raise PaperTradingError("Approval reason must contain 1-512 characters.")
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
                    product_position = connection.execute(
                        "SELECT quantity FROM paper_product_positions "
                        "WHERE account_id = ? AND instrument_id = ? AND product = ?",
                        (account_id, instrument_id, product.value),
                    ).fetchone()
                    product_held = (
                        int(product_position[0]) if product_position is not None else 0
                    )
                    if held < quantity.value:
                        raise PaperTradingError("Sell quantity exceeds held position.")
                    if product_held < quantity.value:
                        raise PaperTradingError(
                            "Sell quantity exceeds the held product position."
                        )
                created_at = _utc(self.clock.now())
                order_id = secrets.token_urlsafe(18)
                approval_status = "PENDING" if require_approval else "APPROVED"
                connection.execute(
                    "INSERT INTO paper_orders "
                    "(order_id, account_id, instrument_id, client_order_id, side, "
                    "order_type, quantity, limit_price_paise, status, "
                    "filled_quantity, created_at, product, approval_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)",
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
                        product.value,
                        approval_status,
                    ),
                )
                if require_approval:
                    connection.execute(
                        "INSERT INTO paper_approvals "
                        "(approval_id, order_id, account_id, status, reason, created_at) "
                        "VALUES (?, ?, ?, 'PENDING', ?, ?)",
                        (
                            secrets.token_urlsafe(18),
                            order_id,
                            account_id,
                            approval_reason,
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
                        "product": product.value,
                        "approval_status": approval_status,
                    },
                )
                return self._get_order(connection, order_id)

    def _fills_for_cost(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        instrument_id: str,
        *,
        product: PaperProduct | None = None,
    ) -> list[tuple[str, str, int, int]]:
        if product is None:
            rows = connection.execute(
                "SELECT f.fill_id, o.side, f.quantity, f.price_paise "
                "FROM paper_fills f JOIN paper_orders o ON o.order_id = f.order_id "
                "WHERE o.account_id = ? AND o.instrument_id = ? "
                "ORDER BY f.filled_at, f.rowid",
                (account_id, instrument_id),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT f.fill_id, o.side, f.quantity, f.price_paise "
                "FROM paper_fills f JOIN paper_orders o ON o.order_id = f.order_id "
                "WHERE o.account_id = ? AND o.instrument_id = ? AND o.product = ? "
                "ORDER BY f.filled_at, f.rowid",
                (account_id, instrument_id, product.value),
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

    @staticmethod
    def _charge_map(
        connection: sqlite3.Connection, fill_ids: list[str]
    ) -> dict[str, int]:
        if not fill_ids:
            return {}
        placeholders = ",".join("?" for _ in fill_ids)
        rows = connection.execute(
            f"SELECT fill_id, SUM(amount_paise) FROM paper_charges "
            f"WHERE fill_id IN ({placeholders}) GROUP BY fill_id",
            tuple(fill_ids),
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    @classmethod
    def _net_for_fill(
        cls,
        rows: list[tuple[str, str, int, int]],
        charges: dict[str, int],
        *,
        target_fill_id: str,
    ) -> int | None:
        held = 0
        cost = 0
        buy_charges = 0
        net: int | None = None
        for fill_id, side, quantity, price_paise in rows:
            if side == "BUY":
                held += quantity
                cost += quantity * price_paise
                buy_charges += charges.get(fill_id, 0)
                continue
            if quantity > held:
                raise PaperTradingError("Existing sell fills have no cost basis.")
            proceeds = quantity * price_paise
            removed = cost if quantity == held else cost * quantity // held
            allocated_charges = (
                buy_charges if quantity == held else buy_charges * quantity // held
            )
            net = proceeds - removed - allocated_charges - charges.get(fill_id, 0)
            if quantity == held:
                held = 0
                cost = 0
                buy_charges = 0
            else:
                cost -= removed
                held -= quantity
                buy_charges -= allocated_charges
            if fill_id == target_fill_id:
                return net
        return None

    @staticmethod
    def _next_weekday(day: date) -> date:
        cursor = day + timedelta(days=1)
        while cursor.weekday() >= 5:
            cursor += timedelta(days=1)
        return cursor

    def _settles_on(self, product: PaperProduct, acquired: date) -> date:
        return acquired if product is PaperProduct.MIS else self._next_weekday(acquired)

    def _consume_lots(
        self,
        connection: sqlite3.Connection,
        order: PaperOrder,
        quantity: int,
        trade_date: date,
    ) -> None:
        rows = connection.execute(
            "SELECT lot_id, remaining_quantity, settles_on FROM paper_tax_lots "
            "WHERE account_id = ? AND instrument_id = ? AND product = ? "
            "AND remaining_quantity > 0 ORDER BY acquired_at, rowid",
            (order.account_id, order.instrument_id, order.product.value),
        ).fetchall()
        if not rows:
            return
        available = sum(
            int(row[1]) for row in rows if date.fromisoformat(str(row[2])) <= trade_date
        )
        if available < quantity:
            raise PaperTradingError(
                "T+1 settlement has not released enough of this CAS position."
            )
        remaining = quantity
        for lot_id, lot_quantity, settles_on in rows:
            if remaining == 0:
                break
            if date.fromisoformat(str(settles_on)) > trade_date:
                continue
            take = min(remaining, int(lot_quantity))
            connection.execute(
                "UPDATE paper_tax_lots SET remaining_quantity = remaining_quantity - ? "
                "WHERE lot_id = ?",
                (take, str(lot_id)),
            )
            remaining -= take

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
                if order.approval_status != "APPROVED":
                    raise PaperTradingError("Order is waiting for owner approval.")
                existing = connection.execute(
                    "SELECT fill_id, quantity, price_paise, filled_at, "
                    "idempotency_key, gross_paise, charges_paise, net_pnl_paise "
                    "FROM paper_fills WHERE order_id = ? AND idempotency_key = ?",
                    (order_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    existing_id = str(existing[0])
                    duplicate_realized = None
                    duplicate_net = (
                        int(existing[7]) if existing[7] is not None else None
                    )
                    if order.side is PaperSide.SELL:
                        rows = self._fills_for_cost(
                            connection,
                            order.account_id,
                            order.instrument_id,
                            product=order.product,
                        )
                        charge_map = self._charge_map(
                            connection, [item[0] for item in rows]
                        )
                        duplicate_realized = self._realized_for_fill(
                            rows, target_fill_id=existing_id
                        )
                        if duplicate_net is None:
                            duplicate_net = self._net_for_fill(
                                rows, charge_map, target_fill_id=existing_id
                            )
                    fill = PaperFill(
                        fill_id=existing_id,
                        order_id=order_id,
                        quantity=int(existing[1]),
                        price_paise=int(existing[2]),
                        filled_at=_parse_timestamp(str(existing[3])),
                        idempotency_key=str(existing[4]),
                        realized_pnl_paise=duplicate_realized,
                        gross_paise=int(existing[5]),
                        charges_paise=int(existing[6]),
                        net_pnl_paise=duplicate_net,
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

                filled_at = _utc(self.clock.now())
                trade_date = filled_at.astimezone(IST).date()
                if order.side is PaperSide.SELL:
                    self._consume_lots(connection, order, quantity.value, trade_date)

                realized: int | None = None
                if order.side is PaperSide.SELL:
                    held, cost = self._basis(
                        self._fills_for_cost(
                            connection,
                            order.account_id,
                            order.instrument_id,
                            product=order.product,
                        )
                    )
                    realized = self._realized_for_sell(
                        held, cost, quantity.value, price_paise.value
                    )

                charges: ChargeBreakdown = compute_charges(
                    side=order.side.value,
                    product=order.product.value,
                    quantity=quantity.value,
                    price_paise=price_paise.value,
                    schedule=self.charge_schedule,
                )
                gross = quantity.value * price_paise.value
                fill_id = secrets.token_urlsafe(18)
                connection.execute(
                    "INSERT INTO paper_fills "
                    "(fill_id, order_id, quantity, price_paise, filled_at, "
                    "idempotency_key, gross_paise, charges_paise, net_pnl_paise) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        fill_id,
                        order_id,
                        quantity.value,
                        price_paise.value,
                        _timestamp(filled_at),
                        idempotency_key,
                        gross,
                        charges.total_paise,
                    ),
                )
                for component in charges.components:
                    connection.execute(
                        "INSERT INTO paper_charges "
                        "(charge_id, fill_id, code, amount_paise) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            secrets.token_urlsafe(18),
                            fill_id,
                            component.code,
                            component.amount_paise,
                        ),
                    )

                if order.side is PaperSide.BUY:
                    connection.execute(
                        "INSERT INTO paper_tax_lots "
                        "(lot_id, account_id, instrument_id, product, buy_fill_id, "
                        "quantity, remaining_quantity, cost_paise, charges_paise, "
                        "acquired_at, settles_on) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            secrets.token_urlsafe(18),
                            order.account_id,
                            order.instrument_id,
                            order.product.value,
                            fill_id,
                            quantity.value,
                            quantity.value,
                            gross,
                            charges.total_paise,
                            _timestamp(filled_at),
                            self._settles_on(order.product, trade_date).isoformat(),
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

                product_row = connection.execute(
                    "SELECT quantity FROM paper_product_positions "
                    "WHERE account_id = ? AND instrument_id = ? AND product = ?",
                    (order.account_id, order.instrument_id, order.product.value),
                ).fetchone()
                product_quantity = int(product_row[0]) if product_row is not None else 0
                product_quantity += (
                    quantity.value if order.side is PaperSide.BUY else -quantity.value
                )
                if product_quantity < 0:
                    raise PaperTradingError(
                        "Fill would create a short product position."
                    )
                if product_row is None:
                    connection.execute(
                        "INSERT INTO paper_product_positions "
                        "(account_id, instrument_id, product, quantity, revision) "
                        "VALUES (?, ?, ?, ?, 1)",
                        (
                            order.account_id,
                            order.instrument_id,
                            order.product.value,
                            product_quantity,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE paper_product_positions "
                        "SET quantity = ?, revision = revision + 1 "
                        "WHERE account_id = ? AND instrument_id = ? AND product = ?",
                        (
                            product_quantity,
                            order.account_id,
                            order.instrument_id,
                            order.product.value,
                        ),
                    )

                cash_amount = (
                    gross + charges.total_paise
                    if order.side is PaperSide.BUY
                    else gross - charges.total_paise
                )
                if cash_amount <= 0:
                    raise PaperTradingError("Charges exceed the sell proceeds.")
                kind = "DEBIT" if order.side is PaperSide.BUY else "CREDIT"
                balance = self.accounts.balance(connection, order.account_id)
                if kind == "DEBIT" and cash_amount > balance.balance_paise:
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
                        cash_amount,
                        _timestamp(filled_at),
                        fill_id,
                    ),
                )

                net_pnl: int | None = None
                if order.side is PaperSide.SELL:
                    rows = self._fills_for_cost(
                        connection,
                        order.account_id,
                        order.instrument_id,
                        product=order.product,
                    )
                    net_pnl = self._net_for_fill(
                        rows,
                        self._charge_map(connection, [item[0] for item in rows]),
                        target_fill_id=fill_id,
                    )
                    connection.execute(
                        "UPDATE paper_fills SET net_pnl_paise = ? WHERE fill_id = ?",
                        (net_pnl, fill_id),
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
                        "product": order.product.value,
                        "gross_paise": str(gross),
                        "charges_paise": str(charges.total_paise),
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
                    gross_paise=gross,
                    charges_paise=charges.total_paise,
                    net_pnl_paise=net_pnl,
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
            charges_row = connection.execute(
                "SELECT COALESCE(SUM(c.amount_paise), 0) FROM paper_charges c "
                "JOIN paper_fills f ON f.fill_id = c.fill_id "
                "JOIN paper_orders o ON o.order_id = f.order_id "
                "WHERE o.account_id = ?",
                (account.account_id,),
            ).fetchone()
            return PaperReconciliation(
                account_id=account.account_id,
                order_count=len(orders),
                fill_count=len(fills),
                ledger_debit_paise=int(ledger[0]),
                ledger_credit_paise=int(ledger[1]),
                position_count=position_count,
                charges_paise=int(charges_row[0]),
            )

    def product_positions(self, account_id: str) -> tuple[ProductPosition, ...]:
        _validate_identifier(account_id, "paper account identifier")
        with closing(self._connect()) as connection:
            self.accounts._get(connection, account_id)
            rows = connection.execute(
                "SELECT account_id, instrument_id, product, quantity "
                "FROM paper_product_positions WHERE account_id = ? AND quantity > 0 "
                "ORDER BY instrument_id, product",
                (account_id,),
            ).fetchall()
        return tuple(
            ProductPosition(
                account_id=str(row[0]),
                instrument_id=str(row[1]),
                product=PaperProduct(str(row[2])),
                quantity=int(row[3]),
            )
            for row in rows
        )

    def tax_lots(
        self, account_id: str, *, instrument_id: str | None = None
    ) -> tuple[PaperTaxLot, ...]:
        _validate_identifier(account_id, "paper account identifier")
        if instrument_id is not None:
            _validate_identifier(instrument_id, "instrument identifier")
        with closing(self._connect()) as connection:
            self.accounts._get(connection, account_id)
            if instrument_id is None:
                rows = connection.execute(
                    "SELECT lot_id, account_id, instrument_id, product, buy_fill_id, "
                    "quantity, remaining_quantity, cost_paise, charges_paise, "
                    "acquired_at, settles_on FROM paper_tax_lots "
                    "WHERE account_id = ? AND remaining_quantity > 0 "
                    "ORDER BY acquired_at, rowid",
                    (account_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT lot_id, account_id, instrument_id, product, buy_fill_id, "
                    "quantity, remaining_quantity, cost_paise, charges_paise, "
                    "acquired_at, settles_on FROM paper_tax_lots "
                    "WHERE account_id = ? AND instrument_id = ? "
                    "AND remaining_quantity > 0 ORDER BY acquired_at, rowid",
                    (account_id, instrument_id),
                ).fetchall()
        return tuple(
            PaperTaxLot(
                lot_id=str(row[0]),
                account_id=str(row[1]),
                instrument_id=str(row[2]),
                product=PaperProduct(str(row[3])),
                buy_fill_id=str(row[4]),
                quantity=int(row[5]),
                remaining_quantity=int(row[6]),
                cost_paise=int(row[7]),
                charges_paise=int(row[8]),
                acquired_at=_parse_timestamp(str(row[9])),
                settles_on=date.fromisoformat(str(row[10])),
            )
            for row in rows
        )

    def approvals(
        self, account_id: str, *, status: str | None = None
    ) -> tuple[PaperApproval, ...]:
        _validate_identifier(account_id, "paper account identifier")
        if status is not None and status not in {"PENDING", "APPROVED", "REJECTED"}:
            raise PaperTradingError("Invalid approval status.")
        with closing(self._connect()) as connection:
            self.accounts._get(connection, account_id)
            if status is None:
                rows = connection.execute(
                    "SELECT approval_id, order_id, account_id, status, reason, "
                    "created_at, decided_at, decision_note FROM paper_approvals "
                    "WHERE account_id = ? ORDER BY created_at DESC, approval_id",
                    (account_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT approval_id, order_id, account_id, status, reason, "
                    "created_at, decided_at, decision_note FROM paper_approvals "
                    "WHERE account_id = ? AND status = ? "
                    "ORDER BY created_at DESC, approval_id",
                    (account_id, status),
                ).fetchall()
        return tuple(
            PaperApproval(
                approval_id=str(row[0]),
                order_id=str(row[1]),
                account_id=str(row[2]),
                status=str(row[3]),
                reason=str(row[4]),
                created_at=_parse_timestamp(str(row[5])),
                decided_at=(
                    _parse_timestamp(str(row[6])) if row[6] is not None else None
                ),
                decision_note=str(row[7]) if row[7] is not None else None,
            )
            for row in rows
        )

    def decide_approval(
        self, order_id: str, *, approved: bool, note: str = ""
    ) -> PaperOrder:
        _validate_identifier(order_id, "paper order identifier")
        if not isinstance(approved, bool):
            raise PaperTradingError("Approval decision must be a boolean.")
        if not isinstance(note, str) or len(note) > 512:
            raise PaperTradingError("Approval note is too long.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                order = self._get_order(connection, order_id)
                if order.approval_status != "PENDING":
                    raise PaperTradingError("Order is not waiting for approval.")
                approval = connection.execute(
                    "SELECT approval_id FROM paper_approvals "
                    "WHERE order_id = ? AND status = 'PENDING'",
                    (order_id,),
                ).fetchone()
                if approval is None:
                    raise PaperTradingError("Pending approval not found.")
                now = _utc(self.clock.now())
                decision = "APPROVED" if approved else "REJECTED"
                connection.execute(
                    "UPDATE paper_approvals SET status = ?, decided_at = ?, "
                    "decision_note = ? WHERE approval_id = ?",
                    (decision, _timestamp(now), note or None, str(approval[0])),
                )
                connection.execute(
                    "UPDATE paper_orders SET approval_status = ?, "
                    "status = CASE WHEN ? = 'REJECTED' THEN 'CANCELLED' ELSE status END "
                    "WHERE order_id = ?",
                    (decision, decision, order_id),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="paper.order.approval_decided",
                    object_type="paper_order",
                    object_id=order_id,
                    payload={"decision": decision, "note": note},
                )
                return self._get_order(connection, order_id)

    def _get_order(self, connection: sqlite3.Connection, order_id: str) -> PaperOrder:
        row = connection.execute(
            "SELECT order_id, account_id, instrument_id, client_order_id, side, "
            "order_type, quantity, limit_price_paise, status, filled_quantity, "
            "created_at, product, approval_status "
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
            product=PaperProduct(str(row[11])),
            approval_status=str(row[12]),
        )
