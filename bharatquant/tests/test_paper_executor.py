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
from bq.core import CurrencyPaise
from bq.execution.paper_executor import (
    ExecutionContext,
    PaperExecutor,
    PaperExecutorError,
    _price_paise,
    assess_freshness,
)
from bq.infra.database import migrate
from bq.paper_accounts import PaperAccountStore
from bq.paper_trading import PaperOrderStatus, PaperOrderType
from bq.risk.policy import (
    KillSwitchLevel,
    RiskDecisionState,
    RiskLimits,
)

AS_OF = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)


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
        "as_of": AS_OF,
    }
    values.update(overrides)
    return TradePlan(**values)  # type: ignore[arg-type]


def _context(**overrides: object) -> ExecutionContext:
    values: dict[str, object] = {
        "account_id": "paper-main",
        "available_cash_paise": 1_000_000,
        "open_positions": 1,
        "symbol_exposure_paise": 0,
        "daily_loss_paise": 0,
        "weekly_loss_paise": 0,
        "monthly_loss_paise": 0,
        "orders_this_minute": 1,
        "kill_switch": KillSwitchLevel.NONE,
        "instrument_valid": True,
        "session_open": True,
    }
    values.update(overrides)
    return ExecutionContext(**values)  # type: ignore[arg-type]


def _limits() -> RiskLimits:
    return RiskLimits(
        max_symbol_exposure_paise=500_000,
        max_open_positions=5,
        max_daily_loss_paise=50_000,
        max_weekly_loss_paise=100_000,
        max_monthly_loss_paise=200_000,
        max_order_rate_per_minute=10,
    )


def _executor(tmp_path: Path, at: datetime = AS_OF) -> PaperExecutor:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock(at)
    accounts = PaperAccountStore(path, clock)
    accounts.create(
        account_id="paper-main",
        name="Paper-Main",
        opening_balance_paise=CurrencyPaise(1_000_000),
    )
    return PaperExecutor(path, limits=_limits(), clock=clock)


# -- freshness mapping ------------------------------------------------------


def test_price_conversion_rejects_a_non_positive_price() -> None:
    # The executor used to accept any Decimal; conversion is now shared with
    # the trade-plan boundary, which rejects non-positive prices.
    with pytest.raises(PaperExecutorError):
        _price_paise(Decimal("-1"))


def test_realtime_fresh_data_is_usable() -> None:
    plan = _plan(data_freshness=DataFreshness.REALTIME_KITE)
    assessment = assess_freshness(plan, now=AS_OF + timedelta(seconds=10))
    assert assessment.usable is True
    assert assessment.label == "realtime"


def test_delayed_fresh_data_is_usable_but_warned() -> None:
    plan = _plan(data_freshness=DataFreshness.DELAYED_YAHOO)
    assessment = assess_freshness(plan, now=AS_OF + timedelta(minutes=5))
    assert assessment.usable is True
    assert assessment.label == "delayed_warn"


def test_critically_stale_data_blocks_even_if_realtime() -> None:
    plan = _plan(data_freshness=DataFreshness.REALTIME_KITE)
    # Ten weekday sessions behind, against a budget of two.
    assessment = assess_freshness(
        plan, now=AS_OF + timedelta(days=10), max_stale_weekdays=2
    )
    assert assessment.usable is False
    assert assessment.label == "stale_block"


def test_swing_freshness_counts_weekday_sessions_not_calendar_days() -> None:
    """A Friday close must still be usable on Monday morning.

    A calendar threshold blocked every Monday, because two weekend days have no
    session at all. Friday is the freshest data that can exist on Monday.
    """
    friday = datetime(2026, 11, 20, 3, 45, tzinfo=UTC)  # Friday session open
    plan = _plan(as_of=friday)
    monday_morning = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)

    assessment = assess_freshness(plan, now=monday_morning)
    assert assessment.usable is True
    assert assessment.label == "delayed_warn"


def test_a_week_of_missed_sessions_blocks_a_swing_plan() -> None:
    friday = datetime(2026, 11, 20, 3, 45, tzinfo=UTC)
    plan = _plan(as_of=friday)
    # Five trading days later (next Friday) is far beyond the two-session budget.
    later = datetime(2026, 11, 30, 4, 0, tzinfo=UTC)
    assessment = assess_freshness(plan, now=later)
    assert assessment.usable is False
    assert assessment.label == "stale_block"


def test_daily_bar_age_is_measured_from_session_close() -> None:
    """A daily bar is stamped at the open but only settles at the close."""
    from bq.execution.paper_executor import SESSION_LENGTH

    close = AS_OF + SESSION_LENGTH
    # The settlement shift means no weekday boundary is crossed on the same day.
    same_day = assess_freshness(_plan(), now=close + timedelta(hours=1))
    assert same_day.usable is True
    # Without the shift the bar would look a full session older.
    assert same_day.age_seconds < 2 * 3600


def test_intraday_bar_age_is_not_shifted_by_the_session_window() -> None:
    plan = _plan(
        timeframe="intraday",
        data_freshness=DataFreshness.REALTIME_KITE,
        product=Product.MIS,
    )
    # 10 minutes later is still inside the 15-minute window.
    fresh = assess_freshness(plan, now=AS_OF + timedelta(minutes=10))
    assert fresh.usable is True
    # 16 minutes later is not.
    stale = assess_freshness(plan, now=AS_OF + timedelta(minutes=16))
    assert stale.usable is False


def test_intraday_uses_the_tighter_critical_window() -> None:
    plan = _plan(
        timeframe="intraday",
        data_freshness=DataFreshness.REALTIME_KITE,
        product=Product.MIS,
    )
    assessment = assess_freshness(plan, now=AS_OF + timedelta(minutes=20))
    assert assessment.usable is False
    assert assessment.label == "stale_block"


# -- the four gate block scenarios -----------------------------------------


def test_delayed_data_does_not_block_but_stale_does(tmp_path: Path) -> None:
    """The gate blocks on critically stale data, not on a delayed price."""
    executor = _executor(tmp_path)
    delayed = _plan(data_freshness=DataFreshness.DELAYED_YAHOO)
    result = executor.execute(
        delayed,
        context=_context(),
        client_order_id="intent-delayed",
    )
    assert result.order is not None  # delayed is WARN, so paper execution proceeds

    stale = _plan(
        data_freshness=DataFreshness.DELAYED_YAHOO,
        as_of=AS_OF - timedelta(days=30),
    )
    blocked = executor.execute(
        stale,
        context=_context(),
        client_order_id="intent-stale",
        now=AS_OF,
    )
    assert blocked.decision.state is RiskDecisionState.BLOCK
    assert blocked.order is None
    assert "data_unusable" in blocked.decision.vetoes


def test_out_of_hours_timestamp_blocks(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.execute(
        _plan(),
        context=_context(session_open=False),
        client_order_id="intent-ooh",
    )
    assert result.decision.state is RiskDecisionState.BLOCK
    assert "session_closed" in result.decision.vetoes
    assert result.order is None


def test_excessive_position_size_blocks(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.execute(
        _plan(suggested_quantity=10),  # notional 10 * 10000 = 100000 paise
        context=_context(symbol_exposure_paise=450_000),
        client_order_id="intent-oversize",
    )
    assert result.decision.state is RiskDecisionState.BLOCK
    assert "symbol_exposure" in result.decision.vetoes
    assert result.order is None


def test_breached_daily_drawdown_blocks(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.execute(
        _plan(),
        context=_context(daily_loss_paise=60_000),
        client_order_id="intent-drawdown",
    )
    assert result.decision.state is RiskDecisionState.BLOCK
    assert "daily_loss_limit" in result.decision.vetoes
    assert result.order is None


def test_insufficient_cash_blocks(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.execute(
        _plan(suggested_quantity=500),
        context=_context(available_cash_paise=10_000),
        client_order_id="intent-cash",
    )
    assert result.decision.state is RiskDecisionState.BLOCK
    assert "insufficient_cash" in result.decision.vetoes


# -- happy path -------------------------------------------------------------


def test_allowed_plan_places_a_pending_limit_order(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.execute(
        _plan(),
        context=_context(),
        client_order_id="intent-1",
    )
    assert result.decision.state is RiskDecisionState.ALLOW
    assert result.order is not None
    assert result.order.status is PaperOrderStatus.PENDING
    assert result.order.order_type is PaperOrderType.LIMIT
    assert result.order.quantity == 10
    assert result.order.limit_price_paise == 10_000
    assert result.order.client_order_id == "intent-1"


def test_same_client_order_id_is_idempotent(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    first = executor.execute(_plan(), context=_context(), client_order_id="intent-1")
    second = executor.execute(_plan(), context=_context(), client_order_id="intent-1")
    assert first.order is not None and second.order is not None
    assert first.order.order_id == second.order.order_id


def test_order_respects_the_sizer_quantity_exactly(tmp_path: Path) -> None:
    """The executor never adjusts the plan quantity, it only relays it."""
    executor = _executor(tmp_path)
    result = executor.execute(
        _plan(suggested_quantity=7),
        context=_context(),
        client_order_id="intent-qty",
    )
    assert result.order is not None
    assert result.order.quantity == 7


# -- refusal of unsupported plans -------------------------------------------


def test_sell_plan_is_refused(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    with pytest.raises(PaperExecutorError, match="BUY"):
        executor.execute(
            _plan(
                signal=Signal.SELL,
                stop_loss=Decimal("105.00"),
                target_price=Decimal("85.00"),
            ),
            context=_context(),
            client_order_id="intent-short",
        )


def test_hold_plan_is_refused(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    with pytest.raises(PaperExecutorError, match="BUY"):
        executor.execute(
            _plan(signal=Signal.HOLD),
            context=_context(),
            client_order_id="intent-hold",
        )


def test_non_trade_plan_is_rejected(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    with pytest.raises(PaperExecutorError):
        executor.execute(
            object(),  # type: ignore[arg-type]
            context=_context(),
            client_order_id="intent-bad",
        )


def test_execution_context_rejects_negative_values() -> None:
    with pytest.raises(PaperExecutorError):
        ExecutionContext(
            account_id="x",
            available_cash_paise=-1,
            open_positions=0,
            symbol_exposure_paise=0,
            daily_loss_paise=0,
            weekly_loss_paise=0,
            monthly_loss_paise=0,
            orders_this_minute=0,
            kill_switch=KillSwitchLevel.NONE,
            instrument_valid=True,
            session_open=True,
        )


def test_execution_context_rejects_a_bad_kill_switch() -> None:
    with pytest.raises(PaperExecutorError):
        ExecutionContext(
            account_id="x",
            available_cash_paise=0,
            open_positions=0,
            symbol_exposure_paise=0,
            daily_loss_paise=0,
            weekly_loss_paise=0,
            monthly_loss_paise=0,
            orders_this_minute=0,
            kill_switch="NONE",  # type: ignore[arg-type]
            instrument_valid=True,
            session_open=True,
        )


def test_freshness_assessment_rejects_a_naive_now() -> None:
    with pytest.raises(PaperExecutorError):
        assess_freshness(_plan(), now=datetime(2026, 11, 25, 10, 0))
