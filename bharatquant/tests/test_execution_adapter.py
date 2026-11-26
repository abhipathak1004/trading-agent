from datetime import UTC, datetime
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
from bq.execution.base import (
    ExecutionDisabled,
    ExecutionMode,
    build_execution_adapter,
)
from bq.execution.paper_executor import ExecutionContext
from bq.infra.database import migrate
from bq.paper_accounts import PaperAccountStore
from bq.paper_trading import PaperTradingStore
from bq.risk.policy import KillSwitchLevel, RiskDecisionState, RiskLimits

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


def _context(**overrides: object) -> ExecutionContext:
    values: dict[str, object] = {
        "account_id": "paper-main",
        "available_cash_paise": 1_000_000,
        "open_positions": 0,
        "symbol_exposure_paise": 0,
        "daily_loss_paise": 0,
        "weekly_loss_paise": 0,
        "monthly_loss_paise": 0,
        "orders_this_minute": 0,
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


def _path(tmp_path: Path) -> tuple[Path, PaperTradingStore]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    PaperAccountStore(path, FrozenClock(NOW)).create(
        account_id="paper-main",
        name="Paper-Main",
        opening_balance_paise=CurrencyPaise(1_000_000),
    )
    return path, PaperTradingStore(path, FrozenClock(NOW))


def test_paper_preview_never_places_an_order(tmp_path: Path) -> None:
    path, trading = _path(tmp_path)
    adapter = build_execution_adapter(ExecutionMode.PAPER, path, limits=_limits())
    outcome = adapter.preview(_plan(), context=_context())
    assert outcome.decision.state is RiskDecisionState.ALLOW
    assert outcome.order is None
    assert trading.recent_orders("paper-main") == ()


def test_paper_place_uses_the_plan_quantity_and_is_idempotent(tmp_path: Path) -> None:
    path, _trading = _path(tmp_path)
    adapter = build_execution_adapter(ExecutionMode.PAPER, path, limits=_limits())
    first = adapter.place(_plan(), context=_context(), client_order_id="intent-1")
    second = adapter.place(_plan(), context=_context(), client_order_id="intent-1")
    assert first.order is not None
    assert first.order.quantity == 10
    assert second.order is not None
    assert second.order.order_id == first.order.order_id


def test_paper_place_blocks_without_placing(tmp_path: Path) -> None:
    path, trading = _path(tmp_path)
    adapter = build_execution_adapter(ExecutionMode.PAPER, path, limits=_limits())
    outcome = adapter.place(
        _plan(),
        context=_context(session_open=False),
        client_order_id="intent-closed",
    )
    assert outcome.decision.state is RiskDecisionState.BLOCK
    assert "session_closed" in outcome.decision.vetoes
    assert outcome.order is None
    assert trading.recent_orders("paper-main") == ()


def test_reward_risk_below_the_code_floor_blocks(tmp_path: Path) -> None:
    path, _trading = _path(tmp_path)
    adapter = build_execution_adapter(ExecutionMode.PAPER, path, limits=_limits())
    thin = _plan(
        target_price=Decimal("100.50"),
        min_reward_risk_bps=0,
    )
    outcome = adapter.preview(thin, context=_context())
    assert outcome.decision.state is RiskDecisionState.BLOCK
    assert "reward_risk" in outcome.decision.vetoes


@pytest.mark.parametrize(
    "mode", [ExecutionMode.KITE_SANDBOX, ExecutionMode.LIVE_APPROVAL]
)
def test_non_paper_modes_fail_closed(tmp_path: Path, mode: ExecutionMode) -> None:
    path, _trading = _path(tmp_path)
    adapter = build_execution_adapter(mode, path, limits=_limits())
    with pytest.raises(ExecutionDisabled):
        adapter.preview(_plan(), context=_context())
    with pytest.raises(ExecutionDisabled):
        adapter.place(_plan(), context=_context(), client_order_id="intent-1")
