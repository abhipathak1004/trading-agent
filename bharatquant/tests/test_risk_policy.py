import pytest

from bq.risk.policy import (
    ApprovalMode,
    ApprovalPolicy,
    KillSwitchLevel,
    RiskDecisionState,
    RiskLimits,
    RiskPolicyError,
    evaluate_pretrade,
)


def test_approval_policy_uses_most_specific_level() -> None:
    policy = ApprovalPolicy(
        global_mode=ApprovalMode.AUTO,
        account_mode=ApprovalMode.APPROVE_ALL,
        horizon_mode=ApprovalMode.APPROVE_ABOVE_THRESHOLD,
        strategy_mode=ApprovalMode.SUGGEST_ONLY,
    )
    assert policy.effective_mode() is ApprovalMode.SUGGEST_ONLY


def test_pretrade_allow_when_all_deterministic_limits_pass() -> None:
    limits = RiskLimits(
        max_symbol_exposure_paise=100_000,
        max_open_positions=5,
        max_daily_loss_paise=10_000,
        max_weekly_loss_paise=30_000,
        max_monthly_loss_paise=80_000,
        max_order_rate_per_minute=10,
    )
    decision = evaluate_pretrade(
        limits,
        symbol_exposure_paise=50_000,
        open_positions=2,
        daily_loss_paise=1_000,
        weekly_loss_paise=5_000,
        monthly_loss_paise=10_000,
        orders_this_minute=2,
        kill_switch=KillSwitchLevel.NONE,
        instrument_valid=True,
        session_open=True,
        stop_set=True,
        data_usable=True,
        available_cash_paise=100_000,
        order_notional_paise=10_000,
    )
    assert decision.state is RiskDecisionState.ALLOW
    assert decision.vetoes == ()


def test_pretrade_blocks_vetoes_and_kill_switch() -> None:
    limits = RiskLimits(
        max_symbol_exposure_paise=20_000,
        max_open_positions=2,
        max_daily_loss_paise=1_000,
        max_weekly_loss_paise=5_000,
        max_monthly_loss_paise=10_000,
        max_order_rate_per_minute=2,
    )
    decision = evaluate_pretrade(
        limits,
        symbol_exposure_paise=20_000,
        open_positions=2,
        daily_loss_paise=1_001,
        weekly_loss_paise=5_001,
        monthly_loss_paise=10_001,
        orders_this_minute=2,
        kill_switch=KillSwitchLevel.HALT_ACCOUNT,
        instrument_valid=False,
        session_open=False,
        stop_set=False,
        data_usable=False,
        available_cash_paise=100,
        order_notional_paise=101,
    )
    assert decision.state is RiskDecisionState.BLOCK
    assert set(decision.vetoes) >= {
        "kill_switch",
        "symbol_exposure",
        "open_position_limit",
        "daily_loss_limit",
        "insufficient_cash",
    }


def test_risk_limits_reject_negative_values() -> None:
    with pytest.raises(RiskPolicyError, match="nonnegative"):
        RiskLimits(
            max_symbol_exposure_paise=-1,
            max_open_positions=5,
            max_daily_loss_paise=1,
            max_weekly_loss_paise=5,
            max_monthly_loss_paise=10,
            max_order_rate_per_minute=2,
        )
