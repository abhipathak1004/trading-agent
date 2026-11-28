"""Account-bound authority, paper only.

The upstream project separates *evidence* from *authority*: a well-supported
research result never widens what may be executed. This module is that gate,
scoped to what this repository permits.

**Paper only.** ADR-001 forbids a live broker path and ADR-013 makes Kite a
read-only data vendor, so `execution_mandates.broker` accepts `PAPER` alone and
there is no `pykiteconnect` write path here to protect. The gate exists so that
when a mandate is missing, revoked or expired, the order never reaches the
executor at all.

**Fail closed, in order:** kill switch, then mandate presence, then broker, then
revocation, then expiry, then symbol allow-list, then single-order notional,
then daily gross exposure. Any failure returns a blocking decision and the
caller's executor is not invoked.

**One audit chain.** Events go to the existing `audit_events` table through
`bq.infra.audit.append_event`; there is deliberately no second ledger.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, TypeVar

from bq.core import Clock, SystemClock, open_connection, parse_timestamp, timestamp, utc
from bq.infra.audit import append_event, verify_chain

KILL_SWITCH_ENV: Final = "BQ_KILL_SWITCH_PATH"
DEFAULT_KILL_SWITCH: Final = "data/KILL_SWITCH"
PAPER_BROKER: Final = "PAPER"

REASON_ALLOWED: Final = "allowed"
REASON_KILL_SWITCH: Final = "kill_switch_active"
REASON_NO_MANDATE: Final = "no_mandate"
REASON_BROKER: Final = "broker_not_paper"
REASON_REVOKED: Final = "mandate_revoked"
REASON_EXPIRED: Final = "mandate_expired"
REASON_SYMBOL: Final = "symbol_not_allowed"
REASON_NOTIONAL: Final = "order_notional_exceeded"
REASON_EXPOSURE: Final = "daily_exposure_exceeded"

T = TypeVar("T")


class MandateError(ValueError):
    """The mandate cannot authorise this operation."""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: int
    price_paise: int

    @property
    def notional_paise(self) -> int:
        return self.quantity * self.price_paise


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ExecutionMandate:
    mandate_id: str
    broker: str
    account_id: str
    allowed_symbols: tuple[str, ...]
    max_order_notional_paise: int
    max_daily_exposure_paise: int
    expires_at: datetime
    revoked: bool
    created_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "mandate_id": self.mandate_id,
            "broker": self.broker,
            "account_id": self.account_id,
            "allowed_symbols": list(self.allowed_symbols),
            "max_order_notional_paise": self.max_order_notional_paise,
            "max_daily_exposure_paise": self.max_daily_exposure_paise,
            "expires_at_utc": timestamp(self.expires_at),
            "revoked": self.revoked,
            "created_at_utc": timestamp(self.created_at),
        }


def kill_switch_path(home: Path | str) -> Path:
    override = os.environ.get(KILL_SWITCH_ENV)
    if override:
        return Path(override).expanduser()
    return Path(home).expanduser() / DEFAULT_KILL_SWITCH


def kill_switch_active(home: Path | str) -> bool:
    """Synchronous: the switch is a file's existence, so there is nothing to await."""
    return kill_switch_path(home).exists()


def gate_order_intent(
    *,
    intent: OrderIntent,
    mandate: ExecutionMandate | None,
    daily_exposure_paise: int,
    now: datetime,
    kill_switch: bool,
) -> GateDecision:
    if kill_switch:
        return GateDecision(False, REASON_KILL_SWITCH)
    if mandate is None:
        return GateDecision(False, REASON_NO_MANDATE)
    if mandate.broker != PAPER_BROKER:
        return GateDecision(False, REASON_BROKER)
    if mandate.revoked:
        return GateDecision(False, REASON_REVOKED)
    if utc(now) >= mandate.expires_at:
        return GateDecision(False, REASON_EXPIRED)
    if intent.symbol not in mandate.allowed_symbols:
        return GateDecision(False, REASON_SYMBOL)
    if intent.quantity <= 0 or intent.price_paise <= 0:
        return GateDecision(False, REASON_NOTIONAL)
    if intent.notional_paise > mandate.max_order_notional_paise:
        return GateDecision(False, REASON_NOTIONAL)
    if daily_exposure_paise + intent.notional_paise > mandate.max_daily_exposure_paise:
        return GateDecision(False, REASON_EXPOSURE)
    return GateDecision(True, REASON_ALLOWED)


def submit_with_mandate(
    intent: OrderIntent,
    *,
    mandate: ExecutionMandate | None,
    daily_exposure_paise: int,
    now: datetime,
    kill_switch: bool,
    execute: Callable[[OrderIntent], T],
) -> tuple[GateDecision, T | None]:
    """The single entry point: a blocked intent never reaches ``execute``."""
    decision = gate_order_intent(
        intent=intent,
        mandate=mandate,
        daily_exposure_paise=daily_exposure_paise,
        now=now,
        kill_switch=kill_switch,
    )
    if not decision.allowed:
        return decision, None
    return decision, execute(intent)


class MandateStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def create(
        self,
        *,
        account_id: str,
        allowed_symbols: Sequence[str],
        max_order_notional_paise: int,
        max_daily_exposure_paise: int,
        expires_at: datetime,
        broker: str = PAPER_BROKER,
    ) -> ExecutionMandate:
        if broker != PAPER_BROKER:
            raise MandateError(
                "Only paper mandates exist in this build; a live broker path is "
                "forbidden by ADR-001."
            )
        if max_order_notional_paise < 0 or max_daily_exposure_paise < 0:
            raise MandateError("Mandate limits must not be negative.")
        symbols = tuple(
            sorted({item.strip() for item in allowed_symbols if item.strip()})
        )
        if not symbols:
            raise MandateError("A mandate needs at least one allowed symbol.")
        mandate_id = "mandate_" + secrets.token_urlsafe(18)
        now = self.clock.now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO execution_mandates (mandate_id, broker, account_id, "
                    "allowed_symbols_json, max_order_notional_paise, "
                    "max_daily_exposure_paise, expires_at_utc, revoked, "
                    "created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (
                        mandate_id,
                        broker,
                        account_id,
                        json.dumps(list(symbols), separators=(",", ":")),
                        max_order_notional_paise,
                        max_daily_exposure_paise,
                        timestamp(expires_at),
                        timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="research.mandate.created",
                    object_type="execution_mandate",
                    object_id=mandate_id,
                    payload={
                        "account_id": account_id,
                        "symbols": str(len(symbols)),
                        "max_order_notional_paise": str(max_order_notional_paise),
                    },
                )
        return self.get(mandate_id)

    def get(self, mandate_id: str) -> ExecutionMandate:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT mandate_id, broker, account_id, allowed_symbols_json, "
                "max_order_notional_paise, max_daily_exposure_paise, expires_at_utc, "
                "revoked, created_at_utc FROM execution_mandates "
                "WHERE mandate_id = ?",
                (mandate_id,),
            ).fetchone()
        if row is None:
            raise MandateError("Mandate not found.")
        try:
            symbols = tuple(str(item) for item in json.loads(str(row[3])))
        except (TypeError, ValueError):
            symbols = ()
        return ExecutionMandate(
            mandate_id=str(row[0]),
            broker=str(row[1]),
            account_id=str(row[2]),
            allowed_symbols=symbols,
            max_order_notional_paise=int(str(row[4])),
            max_daily_exposure_paise=int(str(row[5])),
            expires_at=parse_timestamp(str(row[6])),
            revoked=bool(int(str(row[7]))),
            created_at=parse_timestamp(str(row[8])),
        )

    def active(
        self, account_id: str, *, now: datetime | None = None
    ) -> ExecutionMandate | None:
        moment = utc(now or self.clock.now())
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT mandate_id FROM execution_mandates "
                "WHERE account_id = ? AND revoked = 0 AND expires_at_utc > ? "
                "ORDER BY expires_at_utc DESC",
                (account_id, timestamp(moment)),
            ).fetchall()
        if not rows:
            return None
        return self.get(str(rows[0][0]))

    def revoke(self, mandate_id: str) -> ExecutionMandate:
        now = self.clock.now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "UPDATE execution_mandates SET revoked = 1 WHERE mandate_id = ?",
                    (mandate_id,),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="research.mandate.revoked",
                    object_type="execution_mandate",
                    object_id=mandate_id,
                    payload={},
                )
        return self.get(mandate_id)


def append_audit_event(
    path: Path,
    *,
    event_type: str,
    entity_id: str,
    payload: Mapping[str, object],
    clock: Clock | None = None,
) -> None:
    """Append to the one existing hash chain, never a parallel one."""
    moment = (clock or SystemClock()).now()
    with closing(open_connection(path, foreign_keys=True)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            append_event(
                connection,
                occurred_at=moment,
                actor="owner",
                action=event_type,
                object_type="research_lab",
                object_id=entity_id,
                payload={str(key): str(value) for key, value in payload.items()},
            )


def verify_audit_chain(path: Path) -> bool:
    try:
        with closing(open_connection(path, foreign_keys=False)) as connection:
            verify_chain(connection)
    except Exception:  # noqa: BLE001 - any failure means the chain is not intact
        return False
    return True


__all__ = [
    "DEFAULT_KILL_SWITCH",
    "ExecutionMandate",
    "GateDecision",
    "KILL_SWITCH_ENV",
    "MandateError",
    "MandateStore",
    "OrderIntent",
    "PAPER_BROKER",
    "append_audit_event",
    "gate_order_intent",
    "kill_switch_active",
    "kill_switch_path",
    "submit_with_mandate",
    "verify_audit_chain",
]
