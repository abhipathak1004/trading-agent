import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from bq.core import (
    IDENTIFIER,
    Clock,
    CurrencyPaise,
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


def _utc(value: datetime) -> datetime:
    try:
        return core_utc(value)
    except ValueError as exc:
        raise PaperAccountError("Account timestamps must be timezone-aware.") from exc


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return core_parse_timestamp(value)


LEDGER_TOTALS_SQL = (
    "SELECT "
    "COALESCE(SUM(CASE WHEN kind = 'DEBIT' THEN amount_paise ELSE 0 END), 0), "
    "COALESCE(SUM(CASE WHEN kind = 'CREDIT' THEN amount_paise ELSE 0 END), 0) "
    "FROM paper_ledger_entries WHERE account_id = ?"
)


class PaperAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class PaperAccountError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PaperAccount:
    account_id: str
    name: str
    status: PaperAccountStatus
    opening_balance_paise: int
    created_at: datetime
    revision: int


@dataclass(frozen=True, slots=True)
class PaperAccountMovement:
    movement_id: str
    account_id: str
    kind: str
    amount_paise: int
    occurred_at: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PaperAccountBalance:
    account: PaperAccount
    deposited_paise: int
    withdrawn_paise: int
    balance_paise: int


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise PaperAccountError(f"Invalid {label}.")


class PaperAccountStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=True)

    def create(
        self,
        *,
        account_id: str,
        name: str,
        opening_balance_paise: CurrencyPaise,
        status: PaperAccountStatus = PaperAccountStatus.ACTIVE,
    ) -> PaperAccount:
        _validate_identifier(account_id, "paper account identifier")
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise PaperAccountError("Account name must contain 1–128 characters.")
        if not isinstance(status, PaperAccountStatus):
            raise PaperAccountError("Invalid paper account status.")
        if not isinstance(opening_balance_paise, CurrencyPaise):
            raise TypeError("Opening balance must be CurrencyPaise.")

        created_at = _utc(self.clock.now())
        created_text = _timestamp(created_at)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT 1 FROM paper_accounts WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
                if existing is not None:
                    raise PaperAccountError("Paper account identifier already exists.")
                connection.execute(
                    "INSERT INTO paper_accounts "
                    "(account_id, name, status, opening_balance_paise, created_at, revision) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (
                        account_id,
                        name,
                        status.value,
                        opening_balance_paise.value,
                        created_text,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=created_at,
                    actor="owner",
                    action="paper.account.created",
                    object_type="paper_account",
                    object_id=account_id,
                    config_version="1",
                    payload={
                        "name": name,
                        "opening_balance_paise": str(opening_balance_paise.value),
                    },
                )
                return self._get(connection, account_id)

    def set_status(self, account_id: str, status: PaperAccountStatus) -> PaperAccount:
        _validate_identifier(account_id, "paper account identifier")
        if not isinstance(status, PaperAccountStatus):
            raise PaperAccountError("Invalid paper account status.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                account = self._get(connection, account_id)
                revision = account.revision + 1
                now = _utc(self.clock.now())
                connection.execute(
                    "UPDATE paper_accounts SET status = ?, revision = ? "
                    "WHERE account_id = ?",
                    (status.value, revision, account_id),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="paper.account.status_changed",
                    object_type="paper_account",
                    object_id=account_id,
                    config_version=str(revision),
                    payload={"status": status.value},
                )
                return self._get(connection, account_id)

    def move(
        self,
        account_id: str,
        *,
        kind: str,
        amount_paise: CurrencyPaise,
        idempotency_key: str,
    ) -> PaperAccountMovement:
        _validate_identifier(account_id, "paper account identifier")
        _validate_identifier(idempotency_key, "movement idempotency key")
        if kind not in ("DEPOSIT", "WITHDRAWAL"):
            raise PaperAccountError("Movement kind must be DEPOSIT or WITHDRAWAL.")
        if not isinstance(amount_paise, CurrencyPaise) or amount_paise.value <= 0:
            raise PaperAccountError("Movement amount must be positive CurrencyPaise.")

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                account = self._get(connection, account_id)
                if account.status is not PaperAccountStatus.ACTIVE:
                    raise PaperAccountError(
                        "Capital movements require an active account."
                    )
                existing = connection.execute(
                    "SELECT movement_id, kind, amount_paise, occurred_at, idempotency_key "
                    "FROM paper_account_movements "
                    "WHERE account_id = ? AND idempotency_key = ?",
                    (account_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    return PaperAccountMovement(
                        movement_id=str(existing[0]),
                        account_id=account_id,
                        kind=str(existing[1]),
                        amount_paise=int(existing[2]),
                        occurred_at=_parse_timestamp(str(existing[3])),
                        idempotency_key=str(existing[4]),
                    )
                if kind == "WITHDRAWAL":
                    balance = self.balance(connection, account_id)
                    if amount_paise.value > balance.balance_paise:
                        raise PaperAccountError("Withdrawal exceeds available balance.")
                occurred_at = _utc(self.clock.now())
                movement_id = secrets.token_urlsafe(18)
                connection.execute(
                    "INSERT INTO paper_account_movements "
                    "(movement_id, account_id, kind, amount_paise, occurred_at, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        movement_id,
                        account_id,
                        kind,
                        amount_paise.value,
                        _timestamp(occurred_at),
                        idempotency_key,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=occurred_at,
                    actor="owner",
                    action="paper.account.movement",
                    object_type="paper_account",
                    object_id=account_id,
                    config_version=str(account.revision),
                    payload={
                        "kind": kind,
                        "amount_paise": str(amount_paise.value),
                        "idempotency_key": idempotency_key,
                    },
                )
                return PaperAccountMovement(
                    movement_id=movement_id,
                    account_id=account_id,
                    kind=kind,
                    amount_paise=amount_paise.value,
                    occurred_at=occurred_at,
                    idempotency_key=idempotency_key,
                )

    def get(self, account_id: str) -> PaperAccount:
        _validate_identifier(account_id, "paper account identifier")
        with closing(self._connect()) as connection:
            return self._get(connection, account_id)

    def list_accounts(self, *, limit: int = 50) -> tuple[PaperAccount, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise PaperAccountError("Account limit must be between 1 and 200.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT account_id FROM paper_accounts "
                "ORDER BY created_at DESC, account_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self.get(str(row[0])) for row in rows)

    def get_balance(self, account_id: str) -> PaperAccountBalance:
        _validate_identifier(account_id, "paper account identifier")
        with closing(self._connect()) as connection:
            self._get(connection, account_id)
            return self.balance(connection, account_id)

    def balance(
        self, connection: sqlite3.Connection, account_id: str
    ) -> PaperAccountBalance:
        account = self._get(connection, account_id)
        row = connection.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN kind = 'DEPOSIT' THEN amount_paise ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN kind = 'WITHDRAWAL' THEN amount_paise ELSE 0 END), 0) "
            "FROM paper_account_movements WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        deposited = int(row[0])
        withdrawn = int(row[1])
        ledger_row = connection.execute(LEDGER_TOTALS_SQL, (account_id,)).fetchone()
        ledger_debits = int(ledger_row[0])
        ledger_credits = int(ledger_row[1])
        balance = (
            account.opening_balance_paise
            + deposited
            - withdrawn
            - ledger_debits
            + ledger_credits
        )
        if balance < 0:
            raise PaperAccountError("Paper account balance cannot be negative.")
        return PaperAccountBalance(
            account=account,
            deposited_paise=deposited,
            withdrawn_paise=withdrawn,
            balance_paise=balance,
        )

    def _get(self, connection: sqlite3.Connection, account_id: str) -> PaperAccount:
        row = connection.execute(
            "SELECT account_id, name, status, opening_balance_paise, created_at, revision "
            "FROM paper_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise PaperAccountError("Paper account not found.")
        return PaperAccount(
            account_id=str(row[0]),
            name=str(row[1]),
            status=PaperAccountStatus(str(row[2])),
            opening_balance_paise=int(row[3]),
            created_at=_parse_timestamp(str(row[4])),
            revision=int(row[5]),
        )
