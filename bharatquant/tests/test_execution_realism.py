from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bq.core import CurrencyPaise, Quantity
from bq.data.symbols import Symbol
from bq.data.vendors import (
    Bar,
    BarInterval,
    DataDomain,
    NoMarketDataError,
    Quote,
    VendorRouter,
)
from bq.infra.database import migrate
from bq.paper_accounts import PaperAccountStore
from bq.paper_charges import ChargeSchedule, compute_charges
from bq.paper_square_off import PaperSquareOff
from bq.paper_stops import PaperStopStore
from bq.paper_trading import (
    PaperOrderStatus,
    PaperOrderType,
    PaperProduct,
    PaperSide,
    PaperTradingError,
    PaperTradingStore,
)


class Clock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


class StubVendor:
    name = "stub"
    is_realtime = False
    capabilities = frozenset({DataDomain.BARS_DAILY})

    def __init__(self, bars: tuple[Bar, ...]) -> None:
        self._bars = bars

    def quote(self, symbol: Symbol) -> Quote:
        raise NoMarketDataError(symbol.canonical)

    def bars(
        self,
        symbol: Symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> tuple[Bar, ...]:
        del symbol, interval, start, end
        return self._bars


def _setup(tmp_path: Path, instant: datetime) -> tuple[PaperTradingStore, Clock]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = Clock(instant)
    PaperAccountStore(path, clock).create(
        account_id="paper-main",
        name="Paper Main",
        opening_balance_paise=CurrencyPaise(10_000_000),
    )
    return PaperTradingStore(path, clock), clock


def _buy(
    store: PaperTradingStore,
    *,
    product: PaperProduct = PaperProduct.MIS,
    require_approval: bool = False,
):
    order = store.place_order(
        account_id="paper-main",
        instrument_id="NSE:RELIANCE",
        client_order_id="buy-1",
        side=PaperSide.BUY,
        order_type=PaperOrderType.LIMIT,
        quantity=Quantity(10),
        limit_price_paise=CurrencyPaise(1_000),
        product=product,
        require_approval=require_approval,
    )
    return order


def test_charges_are_itemized_and_net_can_differ_from_gross(tmp_path: Path) -> None:
    instant = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)
    store, clock = _setup(tmp_path, instant)
    buy = _buy(store, product=PaperProduct.CAS)
    _order, buy_fill = store.fill(
        buy.order_id,
        quantity=Quantity(10),
        price_paise=CurrencyPaise(1_000),
        idempotency_key="buy-fill",
    )
    assert buy_fill.gross_paise == 10_000
    assert buy_fill.charges_paise > 0
    assert buy_fill.net_pnl_paise is None

    clock.instant += timedelta(days=1)
    sell = store.place_order(
        account_id="paper-main",
        instrument_id="NSE:RELIANCE",
        client_order_id="sell-1",
        side=PaperSide.SELL,
        order_type=PaperOrderType.LIMIT,
        quantity=Quantity(10),
        limit_price_paise=CurrencyPaise(1_200),
        product=PaperProduct.CAS,
    )
    _order, sell_fill = store.fill(
        sell.order_id,
        quantity=Quantity(10),
        price_paise=CurrencyPaise(1_200),
        idempotency_key="sell-fill",
    )
    assert sell_fill.realized_pnl_paise == 2_000
    assert sell_fill.net_pnl_paise is not None
    assert sell_fill.net_pnl_paise < sell_fill.realized_pnl_paise
    assert sell_fill.charges_paise > 0
    assert store.tax_lots("paper-main") == ()
    reconciled = store.reconcile("paper-main")
    assert reconciled.charges_paise > 0
    assert reconciled.ledger_debit_paise == 10_000 + buy_fill.charges_paise


def test_cas_sell_waits_for_t_plus_one_but_mis_does_not(tmp_path: Path) -> None:
    instant = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)
    store, clock = _setup(tmp_path, instant)
    buy = _buy(store, product=PaperProduct.CAS)
    store.fill(
        buy.order_id,
        quantity=Quantity(10),
        price_paise=CurrencyPaise(1_000),
        idempotency_key="cas-buy",
    )
    sell = store.place_order(
        account_id="paper-main",
        instrument_id="NSE:RELIANCE",
        client_order_id="cas-sell",
        side=PaperSide.SELL,
        order_type=PaperOrderType.LIMIT,
        quantity=Quantity(10),
        limit_price_paise=CurrencyPaise(1_100),
        product=PaperProduct.CAS,
    )
    with pytest.raises(PaperTradingError, match="T\\+1 settlement"):
        store.fill(
            sell.order_id,
            quantity=Quantity(10),
            price_paise=CurrencyPaise(1_100),
            idempotency_key="cash-sell",
        )
    clock.instant += timedelta(days=1)
    _order, fill = store.fill(
        sell.order_id,
        quantity=Quantity(10),
        price_paise=CurrencyPaise(1_100),
        idempotency_key="cash-sell",
    )
    assert fill.realized_pnl_paise == 1_000


def test_approval_blocks_fill_until_the_owner_decides(tmp_path: Path) -> None:
    instant = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)
    store, _clock = _setup(tmp_path, instant)
    order = _buy(store, require_approval=True)
    assert order.approval_status == "PENDING"
    with pytest.raises(PaperTradingError, match="approval"):
        store.fill(
            order.order_id,
            quantity=Quantity(10),
            price_paise=CurrencyPaise(1_000),
            idempotency_key="blocked",
        )
    approvals = store.approvals("paper-main", status="PENDING")
    assert len(approvals) == 1
    approved = store.decide_approval(order.order_id, approved=True, note="checked")
    assert approved.approval_status == "APPROVED"
    _order, fill = store.fill(
        order.order_id,
        quantity=Quantity(10),
        price_paise=CurrencyPaise(1_000),
        idempotency_key="approved-fill",
    )
    assert fill.quantity == 10


def test_stop_triggers_once_from_a_touched_daily_bar(tmp_path: Path) -> None:
    instant = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
    store, clock = _setup(tmp_path, instant)
    buy = _buy(store)
    store.fill(
        buy.order_id,
        quantity=Quantity(10),
        price_paise=CurrencyPaise(1_000),
        idempotency_key="stop-buy",
    )
    stops = PaperStopStore(tmp_path / "bq.db", clock)
    stop = stops.create(
        account_id="paper-main",
        instrument_id="NSE:RELIANCE",
        quantity=Quantity(10),
        stop_price_paise=CurrencyPaise(900),
        target_price_paise=CurrencyPaise(1_200),
        product=PaperProduct.MIS,
    )
    bar = Bar(
        event_at=instant + timedelta(hours=1),
        open=Decimal("10"),
        high=Decimal("10.05"),
        low=Decimal("8.90"),
        close=Decimal("8.95"),
        volume=100,
    )
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, StubVendor((bar,)))
    result = stops.evaluate(router, as_of=instant + timedelta(hours=2))
    assert result.examined == 1
    assert result.triggered == 1
    triggered = stops.get(stop.stop_id)
    assert triggered.status == "TRIGGERED"
    assert triggered.trigger_kind == "STOP"
    assert store.positions("paper-main") == ()
    assert (
        store.get_order(triggered.trigger_order_id or "").status
        is PaperOrderStatus.FILLED
    )
    again = stops.evaluate(router, as_of=instant + timedelta(hours=3))
    assert again.triggered == 0


def test_charge_schedule_has_an_explicit_zero_mode() -> None:
    zero = compute_charges(
        side="SELL",
        product="CAS",
        quantity=10,
        price_paise=1_000,
        schedule=ChargeSchedule.zero(),
    )
    assert zero.total_paise == 0
    normal = compute_charges(side="SELL", product="CAS", quantity=10, price_paise=1_000)
    assert normal.total_paise > 0
    assert {item.code for item in normal.components} <= {
        "STT",
        "EXCHANGE",
        "SEBI",
        "STAMP",
        "GST",
    }


def test_mis_square_off_closes_the_intraday_position(tmp_path: Path) -> None:
    instant = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
    store, clock = _setup(tmp_path, instant)
    buy = _buy(store)
    store.fill(
        buy.order_id,
        quantity=Quantity(10),
        price_paise=CurrencyPaise(1_000),
        idempotency_key="square-buy",
    )
    bar = Bar(
        event_at=instant + timedelta(hours=1),
        open=Decimal("11"),
        high=Decimal("11.2"),
        low=Decimal("10.8"),
        close=Decimal("11"),
        volume=100,
    )
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, StubVendor((bar,)))
    result = PaperSquareOff(tmp_path / "bq.db", clock).square_off(
        router, as_of=instant + timedelta(hours=4), account_id="paper-main"
    )
    assert result.examined == 1
    assert result.squared_off == 1
    assert store.positions("paper-main") == ()
    again = PaperSquareOff(tmp_path / "bq.db", clock).square_off(
        router, as_of=instant + timedelta(hours=5), account_id="paper-main"
    )
    assert again.examined == 0
