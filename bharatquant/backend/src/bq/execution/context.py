"""Read-only account snapshot for the pre-trade gate.

The paper executor is deliberately a pure function of its inputs. This module
assembles those inputs from the existing paper tables in one SQLite snapshot:

* cash is the paper-account balance (opening + deposits - withdrawals - ledger);
* open positions and symbol exposure come from ``paper_positions``;
* realised daily/weekly/monthly losses come from average-cost matched ``paper_fills``;
* order rate comes from ``paper_orders`` in the last minute;
* session state comes from the reviewed exchange calendar.

Only **realised** losses are counted. Unrealised P&L is not included because the
paper tables do not store a mark price. Symbol exposure is marked at the proposed
entry price, which is the only price the caller has at gate time.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bq.analysis.decision import TradePlan
from bq.core import SystemClock, open_connection, paise_from_price
from bq.core import parse_timestamp as core_parse_timestamp
from bq.data.calendar import ExchangeCalendar, SessionState, UncoveredCalendarDate
from bq.data.symbols import SymbolError
from bq.data.symbols import resolve as resolve_symbol
from bq.execution.paper_executor import ExecutionContext
from bq.paper_accounts import PaperAccountStore
from bq.risk.policy import KillSwitchLevel

IST = ZoneInfo("Asia/Kolkata")
MINUTE_SECONDS = 60


class ExecutionContextError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    instrument_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    context: ExecutionContext
    account_name: str
    balance_paise: int
    positions: tuple[PositionSnapshot, ...]
    session_state: str
    session_source: str
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.context.account_id,
            "account_name": self.account_name,
            "available_cash_paise": str(self.context.available_cash_paise),
            "balance_paise": str(self.balance_paise),
            "open_positions": self.context.open_positions,
            "symbol_exposure_paise": str(self.context.symbol_exposure_paise),
            "daily_loss_paise": str(self.context.daily_loss_paise),
            "weekly_loss_paise": str(self.context.weekly_loss_paise),
            "monthly_loss_paise": str(self.context.monthly_loss_paise),
            "orders_this_minute": self.context.orders_this_minute,
            "kill_switch": self.context.kill_switch.value,
            "instrument_valid": self.context.instrument_valid,
            "session_open": self.context.session_open,
            "session_state": self.session_state,
            "session_source": self.session_source,
            "notes": list(self.notes),
            "positions": [
                {"instrument_id": item.instrument_id, "quantity": item.quantity}
                for item in self.positions
            ],
        }


def _realised_losses(
    rows: list[tuple[object, ...]], *, now: datetime
) -> tuple[int, int, int]:
    inventory: dict[str, tuple[int, int]] = {}
    realised: list[tuple[date, int]] = []
    for instrument_id, side, quantity, price_paise, filled_at in rows:
        symbol = str(instrument_id)
        filled = core_parse_timestamp(str(filled_at))
        if filled > now:
            continue
        quantity_value = int(str(quantity))
        price_value = int(str(price_paise))
        held_quantity, held_cost = inventory.get(symbol, (0, 0))
        if str(side) == "BUY":
            inventory[symbol] = (
                held_quantity + quantity_value,
                held_cost + quantity_value * price_value,
            )
            continue
        if quantity_value > held_quantity:
            raise ExecutionContextError(
                f"Paper fills sell more {symbol} than was held."
            )
        proceeds = quantity_value * price_value
        if quantity_value == held_quantity:
            pnl = proceeds - held_cost
            inventory[symbol] = (0, 0)
        else:
            cost_removed = held_cost * quantity_value // held_quantity
            pnl = proceeds - cost_removed
            inventory[symbol] = (
                held_quantity - quantity_value,
                held_cost - cost_removed,
            )
        realised.append((filled.astimezone(IST).date(), pnl))

    today = now.astimezone(IST).date()
    windows = (
        today,
        today - timedelta(days=6),
        today - timedelta(days=29),
    )
    losses: list[int] = []
    for start in windows:
        total = sum(pnl for day, pnl in realised if start <= day <= today and pnl < 0)
        losses.append(abs(total))
    return losses[0], losses[1], losses[2]


def _session_state(
    path: Path,
    plan: TradePlan,
    *,
    now: datetime,
    override: bool | None,
) -> tuple[bool, str, str, list[str]]:
    notes: list[str] = []
    state = "UNCOVERED"
    source = "calendar"
    open_now = False
    try:
        session = ExchangeCalendar(path).session_for(
            plan.exchange, now.astimezone(IST).date(), as_of=now
        )
        state = session.state.value
        if (
            session.state is SessionState.OPEN
            and session.opens_at is not None
            and session.closes_at is not None
        ):
            open_now = session.opens_at <= now <= session.closes_at
    except UncoveredCalendarDate:
        notes.append(
            "Calendar coverage is missing for this session; the gate fails closed "
            "unless the owner explicitly overrides the session flag."
        )
    if override is not None:
        open_now = override
        source = "owner_override"
        notes.append("Session state was supplied by the owner, not the calendar.")
    return open_now, state, source, notes


def build_execution_context(
    path: Path,
    plan: TradePlan,
    *,
    account_id: str,
    now: datetime,
    mark_price_paise: int | None = None,
    kill_switch: KillSwitchLevel = KillSwitchLevel.NONE,
    session_open_override: bool | None = None,
) -> ExecutionSnapshot:
    if not isinstance(plan, TradePlan):
        raise ExecutionContextError("A TradePlan is required.")
    if now.utcoffset() is None:
        raise ExecutionContextError(
            "The execution snapshot needs a timezone-aware now."
        )
    if not isinstance(kill_switch, KillSwitchLevel):
        raise ExecutionContextError("Invalid kill switch level.")
    try:
        mark = (
            mark_price_paise
            if mark_price_paise is not None
            else paise_from_price(plan.entry_price).value
        )
    except ValueError as exc:
        raise ExecutionContextError(str(exc)) from exc
    if isinstance(mark, bool) or not isinstance(mark, int) or mark <= 0:
        raise ExecutionContextError("The mark price must be positive paise.")

    accounts = PaperAccountStore(path, SystemClock())
    now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    minute_text = (
        (now - timedelta(seconds=MINUTE_SECONDS))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with closing(open_connection(path, foreign_keys=True)) as connection:
        connection.execute("BEGIN")
        account = connection.execute(
            "SELECT account_id, name, status FROM paper_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if account is None:
            raise ExecutionContextError("Paper account not found.")
        if str(account[2]) != "ACTIVE":
            raise ExecutionContextError("Paper account is not active.")
        balance = accounts.balance(connection, account_id)
        position_rows = connection.execute(
            "SELECT instrument_id, quantity FROM paper_positions "
            "WHERE account_id = ? AND quantity > 0 ORDER BY instrument_id",
            (account_id,),
        ).fetchall()
        fill_rows = connection.execute(
            "SELECT o.instrument_id, o.side, f.quantity, f.price_paise, f.filled_at "
            "FROM paper_fills f JOIN paper_orders o ON o.order_id = f.order_id "
            "WHERE o.account_id = ? AND f.filled_at <= ? "
            "ORDER BY f.filled_at, f.rowid",
            (account_id, now_text),
        ).fetchall()
        order_rows = connection.execute(
            "SELECT created_at FROM paper_orders "
            "WHERE account_id = ? AND created_at >= ? AND created_at <= ?",
            (account_id, minute_text, now_text),
        ).fetchall()

    positions = tuple(
        PositionSnapshot(instrument_id=str(row[0]), quantity=int(row[1]))
        for row in position_rows
    )
    held = {item.instrument_id: item.quantity for item in positions}
    daily_loss, weekly_loss, monthly_loss = _realised_losses(
        [tuple(row) for row in fill_rows], now=now
    )
    session_open, session_state, session_source, notes = _session_state(
        path, plan, now=now, override=session_open_override
    )
    try:
        resolved = resolve_symbol(plan.symbol)
        instrument_valid = (
            resolved.tradeable and resolved.exchange.value == plan.exchange
        )
    except SymbolError:
        instrument_valid = False
    notes = [
        *notes,
        "Loss limits use realised P&L from paper fills only; unrealised P&L is not included.",
        "Symbol exposure is marked at the proposed entry price, not a live quote.",
    ]
    context = ExecutionContext(
        account_id=account_id,
        available_cash_paise=balance.balance_paise,
        open_positions=len(positions),
        symbol_exposure_paise=held.get(plan.symbol, 0) * mark,
        daily_loss_paise=daily_loss,
        weekly_loss_paise=weekly_loss,
        monthly_loss_paise=monthly_loss,
        orders_this_minute=len(order_rows),
        kill_switch=kill_switch,
        instrument_valid=instrument_valid,
        session_open=session_open,
    )
    return ExecutionSnapshot(
        context=context,
        account_name=str(account[1]),
        balance_paise=balance.balance_paise,
        positions=positions,
        session_state=session_state,
        session_source=session_source,
        notes=tuple(notes),
    )


__all__ = [
    "ExecutionContextError",
    "ExecutionSnapshot",
    "PositionSnapshot",
    "build_execution_context",
]
