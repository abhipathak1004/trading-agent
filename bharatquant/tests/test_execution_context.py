from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bq.analysis.decision import (
    DataFreshness,
    OrderType,
    Product,
    Signal,
    TradePlan,
)
from bq.core import CurrencyPaise, Quantity
from bq.execution.context import ExecutionContextError, build_execution_context
from bq.infra.database import migrate
from bq.paper_accounts import PaperAccountStatus, PaperAccountStore
from bq.paper_trading import PaperOrderType, PaperSide, PaperTradingStore
from bq.risk.policy import KillSwitchLevel

NOW = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def _plan(**overrides: object) -> TradePlan:
    values: dict[str, object] = {
        "signal": Signal.BUY,
        "symbol": "NSE:RELIANCE",
        "exchange": "NSE",
        "product": Product.CNC,
        "order_type": OrderType.LIMIT,
        "entry_price": Decimal("100.00"),
        "stop_loss": Decimal("95.00"),
        "target_price": Decimal("115.00"),
        "suggested_quantity": 10,
        "confidence": 0.6,
        "timeframe": "swing",
        "invalidation_reason": "Closes below 95.",
        "data_freshness": DataFreshness.DELAYED_YAHOO,
        "as_of": NOW,
    }
    values.update(overrides)
    return TradePlan(**values)  # type: ignore[arg-type]


def _setup(tmp_path: Path) -> tuple[Path, PaperTradingStore]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    accounts = PaperAccountStore(path, FrozenClock(NOW - timedelta(hours=3)))
    accounts.create(
        account_id="paper-main",
        name="Paper-Main",
        opening_balance_paise=CurrencyPaise(1_000_000),
    )
    return path, PaperTradingStore(path, FrozenClock(NOW - timedelta(hours=2)))


def test_snapshot_reads_cash_positions_and_realised_loss(tmp_path: Path) -> None:
    path, trading = _setup(tmp_path)
    buy = trading.place_order(
        account_id="paper-main",
        instrument_id="NSE:RELIANCE",
        client_order_id="buy-1",
        side=PaperSide.BUY,
        order_type=PaperOrderType.LIMIT,
        quantity=Quantity(10),
        limit_price_paise=CurrencyPaise(10_000),
    )
    trading.fill(
        buy.order_id,
        quantity=Quantity(10),
        price_paise=CurrencyPaise(10_000),
        idempotency_key="fill-buy-1",
    )
    sell = trading.place_order(
        account_id="paper-main",
        instrument_id="NSE:RELIANCE",
        client_order_id="sell-1",
        side=PaperSide.SELL,
        order_type=PaperOrderType.LIMIT,
        quantity=Quantity(5),
        limit_price_paise=CurrencyPaise(9_000),
    )
    trading.fill(
        sell.order_id,
        quantity=Quantity(5),
        price_paise=CurrencyPaise(9_000),
        idempotency_key="fill-sell-1",
    )

    snapshot = build_execution_context(
        path,
        _plan(),
        account_id="paper-main",
        now=NOW,
        session_open_override=True,
    )
    assert snapshot.context.available_cash_paise == 944_981
    assert snapshot.context.open_positions == 1
    assert snapshot.context.symbol_exposure_paise == 50_000
    assert snapshot.context.daily_loss_paise == 5_000
    assert snapshot.context.weekly_loss_paise == 5_000
    assert snapshot.context.monthly_loss_paise == 5_000
    assert snapshot.context.session_open is True
    assert snapshot.session_source == "owner_override"
    assert snapshot.positions[0].quantity == 5


def test_snapshot_fails_closed_without_calendar_coverage(tmp_path: Path) -> None:
    path, _trading = _setup(tmp_path)
    snapshot = build_execution_context(path, _plan(), account_id="paper-main", now=NOW)
    assert snapshot.context.session_open is False
    assert snapshot.session_state == "UNCOVERED"
    assert any("Calendar coverage" in note for note in snapshot.notes)


def test_snapshot_rejects_an_unknown_account(tmp_path: Path) -> None:
    path, _trading = _setup(tmp_path)
    with pytest.raises(ExecutionContextError, match="not found"):
        build_execution_context(
            path, _plan(), account_id="missing", now=NOW, session_open_override=True
        )


def test_snapshot_marks_an_index_symbol_invalid(tmp_path: Path) -> None:
    path, _trading = _setup(tmp_path)
    snapshot = build_execution_context(
        path,
        _plan(symbol="NIFTY 50"),
        account_id="paper-main",
        now=NOW,
        session_open_override=True,
    )
    assert snapshot.context.instrument_valid is False


def test_snapshot_preserves_the_kill_switch(tmp_path: Path) -> None:
    path, _trading = _setup(tmp_path)
    snapshot = build_execution_context(
        path,
        _plan(),
        account_id="paper-main",
        now=NOW,
        kill_switch=KillSwitchLevel.HALT_ACCOUNT,
        session_open_override=True,
    )
    assert snapshot.context.kill_switch is KillSwitchLevel.HALT_ACCOUNT


def test_snapshot_rejects_a_paused_account(tmp_path: Path) -> None:
    path, _trading = _setup(tmp_path)
    PaperAccountStore(path).set_status("paper-main", PaperAccountStatus.PAUSED)
    with pytest.raises(ExecutionContextError, match="not active"):
        build_execution_context(
            path, _plan(), account_id="paper-main", now=NOW, session_open_override=True
        )
