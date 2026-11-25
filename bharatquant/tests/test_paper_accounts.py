import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.core import CurrencyPaise
from bq.infra.database import migrate
from bq.paper_accounts import (
    PaperAccountError,
    PaperAccountStatus,
    PaperAccountStore,
)


class FrozenClock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


def test_account_balance_deposit_withdrawal_and_idempotency(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = PaperAccountStore(path, clock)

    account = store.create(
        account_id="paper-main",
        name="Paper-Main",
        opening_balance_paise=CurrencyPaise(1_000_000_00),
    )
    assert account.status is PaperAccountStatus.ACTIVE

    first = store.move(
        "paper-main",
        kind="DEPOSIT",
        amount_paise=CurrencyPaise(250_000_00),
        idempotency_key="deposit-1",
    )
    duplicate = store.move(
        "paper-main",
        kind="DEPOSIT",
        amount_paise=CurrencyPaise(250_000_00),
        idempotency_key="deposit-1",
    )
    assert duplicate == first

    withdrawal = store.move(
        "paper-main",
        kind="WITHDRAWAL",
        amount_paise=CurrencyPaise(100_000_00),
        idempotency_key="withdraw-1",
    )
    assert withdrawal.kind == "WITHDRAWAL"
    balance = store.get_balance("paper-main")
    assert balance.deposited_paise == 250_000_00
    assert balance.withdrawn_paise == 100_000_00
    assert balance.balance_paise == 1_150_000_00


def test_account_rejects_overdraw_and_paused_movement(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = PaperAccountStore(path)
    store.create(
        account_id="paper-main",
        name="Paper-Main",
        opening_balance_paise=CurrencyPaise(100),
    )

    with pytest.raises(PaperAccountError, match="exceeds"):
        store.move(
            "paper-main",
            kind="WITHDRAWAL",
            amount_paise=CurrencyPaise(101),
            idempotency_key="too-large",
        )

    store.set_status("paper-main", PaperAccountStatus.PAUSED)
    with pytest.raises(PaperAccountError, match="active"):
        store.move(
            "paper-main",
            kind="DEPOSIT",
            amount_paise=CurrencyPaise(1),
            idempotency_key="paused",
        )


def test_paper_account_status_is_authenticated_and_exact(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    auth.setup("synthetic owner password")
    store = PaperAccountStore(path)
    store.create(
        account_id="paper-main",
        name="Paper-Main",
        opening_balance_paise=CurrencyPaise(1_000_000_00),
    )
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            unauthorized = await client.get("/api/v1/paper/accounts/paper-main")
            assert unauthorized.status_code == 401
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": "synthetic owner password"},
            )
            assert login.status_code == 200
            response = await client.get("/api/v1/paper/accounts/paper-main")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["balance_paise"] == "100000000"

    asyncio.run(check())
