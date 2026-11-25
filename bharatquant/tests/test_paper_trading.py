from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.core import CurrencyPaise, Quantity
from bq.infra.database import migrate
from bq.paper_accounts import PaperAccountStore
from bq.paper_trading import (
    PaperOrderStatus,
    PaperOrderType,
    PaperSide,
    PaperTradingError,
    PaperTradingStore,
)


class FrozenClock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


def test_limit_buy_fill_position_cash_and_idempotency(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    accounts = PaperAccountStore(path, clock)
    accounts.create(
        account_id="paper-main",
        name="Paper-Main",
        opening_balance_paise=CurrencyPaise(100_000),
    )
    store = PaperTradingStore(path, clock)

    order = store.place_order(
        account_id="paper-main",
        instrument_id="NSE:RELIANCE",
        client_order_id="intent-1",
        side=PaperSide.BUY,
        order_type=PaperOrderType.LIMIT,
        quantity=Quantity(10),
        limit_price_paise=CurrencyPaise(1_000),
    )
    assert order.status is PaperOrderStatus.PENDING

    first_order, fill = store.fill(
        order.order_id,
        quantity=Quantity(10),
        price_paise=CurrencyPaise(950),
        idempotency_key="fill-1",
    )
    duplicate_order, duplicate_fill = store.fill(
        order.order_id,
        quantity=Quantity(10),
        price_paise=CurrencyPaise(950),
        idempotency_key="fill-1",
    )
    assert first_order.status is PaperOrderStatus.FILLED
    assert duplicate_order.status is first_order.status
    assert duplicate_fill == fill

    balance = accounts.get_balance("paper-main")
    assert balance.balance_paise == 90_500

    with closing(store._connect()) as connection:
        position = connection.execute(
            "SELECT quantity FROM paper_positions "
            "WHERE account_id = ? AND instrument_id = ?",
            ("paper-main", "NSE:RELIANCE"),
        ).fetchone()
        assert position == (10,)
    reconciliation = store.reconcile("paper-main")
    assert reconciliation.order_count == 1
    assert reconciliation.fill_count == 1
    assert reconciliation.ledger_debit_paise == 9_500
    assert reconciliation.position_count == 1


def test_limit_violation_short_and_cancel(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    accounts = PaperAccountStore(path, clock)
    accounts.create(
        account_id="paper-main",
        name="Paper-Main",
        opening_balance_paise=CurrencyPaise(100_000),
    )
    store = PaperTradingStore(path, clock)
    buy = store.place_order(
        account_id="paper-main",
        instrument_id="NSE:INFY",
        client_order_id="buy-1",
        side=PaperSide.BUY,
        order_type=PaperOrderType.LIMIT,
        quantity=Quantity(1),
        limit_price_paise=CurrencyPaise(1_000),
    )
    with pytest.raises(PaperTradingError, match="limit price"):
        store.fill(
            buy.order_id,
            quantity=Quantity(1),
            price_paise=CurrencyPaise(1_001),
            idempotency_key="bad-fill",
        )

    with pytest.raises(PaperTradingError, match="held position"):
        store.place_order(
            account_id="paper-main",
            instrument_id="NSE:INFY",
            client_order_id="sell-2",
            side=PaperSide.SELL,
            order_type=PaperOrderType.MARKET,
            quantity=Quantity(1),
        )
    cancelled = store.cancel(buy.order_id)
    assert cancelled.status is PaperOrderStatus.CANCELLED
