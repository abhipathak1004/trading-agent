from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bq.analysis.decision import (
    DataFreshness,
    OrderType,
    Product,
    Signal,
    TradePlan,
    TradePlanError,
    build_plan,
    verify_quantity_matches_sizing,
)

AS_OF = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
EQUITY = 1_000_000_00  # ₹10,00,000 in paise
CASH = 500_000_00
RISK_BPS = 100  # 1%


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
        "invalidation_reason": "Closes below the 95 swing low.",
        "data_freshness": DataFreshness.DELAYED_YAHOO,
        "as_of": AS_OF,
    }
    values.update(overrides)
    return TradePlan(**values)  # type: ignore[arg-type]


# -- structure -------------------------------------------------------------


def test_a_valid_plan_constructs() -> None:
    plan = _plan()
    assert plan.reward_risk == pytest.approx(3.0)
    assert plan.risk_per_share == Decimal("5.00")


def test_only_limit_orders_are_permitted() -> None:
    with pytest.raises(TradePlanError):
        _plan(order_type="MARKET")


def test_long_stop_must_be_below_entry() -> None:
    with pytest.raises(TradePlanError):
        _plan(stop_loss=Decimal("105.00"))


def test_long_target_must_be_above_entry() -> None:
    with pytest.raises(TradePlanError):
        _plan(target_price=Decimal("99.00"))


def test_short_side_direction_is_checked() -> None:
    with pytest.raises(TradePlanError):
        _plan(signal=Signal.SELL, stop_loss=Decimal("95.00"))


def test_a_short_plan_can_be_constructed_when_direction_is_right() -> None:
    plan = _plan(
        signal=Signal.SELL,
        stop_loss=Decimal("105.00"),
        target_price=Decimal("85.00"),
    )
    assert plan.signal is Signal.SELL


def test_quantity_must_be_positive() -> None:
    with pytest.raises(TradePlanError):
        _plan(suggested_quantity=0)
    with pytest.raises(TradePlanError):
        _plan(suggested_quantity=-5)


def test_quantity_must_be_an_integer_not_a_bool() -> None:
    with pytest.raises(TradePlanError):
        _plan(suggested_quantity=True)


def test_confidence_is_bounded() -> None:
    with pytest.raises(TradePlanError):
        _plan(confidence=1.5)
    with pytest.raises(TradePlanError):
        _plan(confidence=-0.1)


def test_symbol_and_exchange_are_validated() -> None:
    with pytest.raises(TradePlanError):
        _plan(symbol="")
    with pytest.raises(TradePlanError):
        _plan(exchange="NASDAQ")


def test_as_of_must_be_aware() -> None:
    with pytest.raises(TradePlanError):
        _plan(as_of=datetime(2026, 11, 25, 10, 0))


def test_an_invalidation_reason_is_required() -> None:
    with pytest.raises(TradePlanError):
        _plan(invalidation_reason="")


def test_data_freshness_must_be_declared() -> None:
    with pytest.raises(TradePlanError):
        _plan(data_freshness="live")


# -- reward/risk -----------------------------------------------------------


def test_reward_risk_floor_is_enforced() -> None:
    plan = _plan(target_price=Decimal("106.00"))  # rr = 1.2
    with pytest.raises(TradePlanError):
        plan.check_reward_risk()


def test_reward_risk_floor_can_be_configured() -> None:
    plan = _plan(target_price=Decimal("106.00"), min_reward_risk_bps=10_000)
    plan.check_reward_risk()  # 1.0 floor, rr is 1.2 => passes


def test_zero_stop_distance_is_undefined_not_infinite() -> None:
    # Constructed with equal entry/stop is rejected earlier; a SELL plan with a
    # legitimate-looking but equal stop is also rejected.
    with pytest.raises(TradePlanError):
        _plan(stop_loss=Decimal("100.00"))


def test_notional_uses_entry_and_quantity() -> None:
    plan = _plan()
    assert plan.notional_paise().value == 100 * 100 * 10  # ₹100 -> 10000 paise * 10


# -- sizing integration ----------------------------------------------------


def test_build_plan_uses_the_sizer_and_not_a_model() -> None:
    plan = build_plan(
        signal=Signal.BUY,
        symbol="NSE:RELIANCE",
        exchange="NSE",
        product=Product.CNC,
        entry_price=Decimal("100.00"),
        stop_loss=Decimal("95.00"),
        target_price=Decimal("115.00"),
        confidence=0.6,
        timeframe="swing",
        invalidation_reason="below 95",
        data_freshness=DataFreshness.DELAYED_YAHOO,
        as_of=AS_OF,
        equity_paise=EQUITY,
        available_cash_paise=CASH,
        risk_basis_points=RISK_BPS,
    )
    # Risk budget = 1% of 10,00,000 = 10,000; risk/share = 5 => 2,000 risk-limited.
    # Cash limit = 5,00,000 / 100 = 5,000. Quantity = min = 2,000.
    assert plan.suggested_quantity == 2000
    verify_quantity_matches_sizing(
        plan,
        equity_paise=EQUITY,
        available_cash_paise=CASH,
        risk_basis_points=RISK_BPS,
    )


def test_a_model_inflated_quantity_is_detected() -> None:
    plan = _plan(suggested_quantity=999_999)
    with pytest.raises(TradePlanError):
        verify_quantity_matches_sizing(
            plan,
            equity_paise=EQUITY,
            available_cash_paise=CASH,
            risk_basis_points=RISK_BPS,
        )


def test_build_plan_refuses_zero_size() -> None:
    with pytest.raises(TradePlanError):
        build_plan(
            signal=Signal.BUY,
            symbol="NSE:RELIANCE",
            exchange="NSE",
            product=Product.CNC,
            entry_price=Decimal("10000.00"),
            stop_loss=Decimal("9999.00"),
            target_price=Decimal("10003.00"),
            confidence=0.5,
            timeframe="swing",
            invalidation_reason="x",
            data_freshness=DataFreshness.DELAYED_YAHOO,
            as_of=AS_OF,
            equity_paise=100_00,  # ₹100 equity
            available_cash_paise=100_00,
            risk_basis_points=RISK_BPS,
        )


def test_build_plan_refuses_a_short() -> None:
    with pytest.raises(TradePlanError):
        build_plan(
            signal=Signal.SELL,
            symbol="NSE:X",
            exchange="NSE",
            product=Product.MIS,
            entry_price=Decimal("100.00"),
            stop_loss=Decimal("105.00"),
            target_price=Decimal("90.00"),
            confidence=0.5,
            timeframe="intraday",
            invalidation_reason="x",
            data_freshness=DataFreshness.REALTIME_KITE,
            as_of=AS_OF,
            equity_paise=EQUITY,
            available_cash_paise=CASH,
            risk_basis_points=RISK_BPS,
        )


def test_build_plan_enforces_reward_risk() -> None:
    with pytest.raises(TradePlanError):
        build_plan(
            signal=Signal.BUY,
            symbol="NSE:X",
            exchange="NSE",
            product=Product.CNC,
            entry_price=Decimal("100.00"),
            stop_loss=Decimal("95.00"),
            target_price=Decimal("101.00"),
            confidence=0.5,
            timeframe="swing",
            invalidation_reason="x",
            data_freshness=DataFreshness.DELAYED_YAHOO,
            as_of=AS_OF,
            equity_paise=EQUITY,
            available_cash_paise=CASH,
            risk_basis_points=RISK_BPS,
        )


def test_plan_is_json_safe() -> None:
    import json

    payload = json.loads(json.dumps(_plan().to_dict()))
    assert payload["signal"] == "BUY"
    assert payload["order_type"] == "LIMIT"
    assert payload["data_freshness"] == "delayed_yfinance"
