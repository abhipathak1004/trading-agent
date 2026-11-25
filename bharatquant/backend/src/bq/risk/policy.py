from dataclasses import dataclass
from enum import StrEnum


class ApprovalMode(StrEnum):
    AUTO = "AUTO"
    APPROVE_ABOVE_THRESHOLD = "APPROVE_ABOVE_THRESHOLD"
    APPROVE_ALL = "APPROVE_ALL"
    SUGGEST_ONLY = "SUGGEST_ONLY"


class KillSwitchLevel(StrEnum):
    NONE = "NONE"
    PAUSE_STRATEGY = "PAUSE_STRATEGY"
    FLATTEN_STRATEGY = "FLATTEN_STRATEGY"
    PAUSE_HORIZON = "PAUSE_HORIZON"
    HALT_ACCOUNT = "HALT_ACCOUNT"
    FLATTEN_ALL = "FLATTEN_ALL"


class RiskDecisionState(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


class RiskPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    global_mode: ApprovalMode
    account_mode: ApprovalMode | None = None
    horizon_mode: ApprovalMode | None = None
    strategy_mode: ApprovalMode | None = None
    threshold_notional_paise: int | None = None
    threshold_risk_bps: int | None = None

    def __post_init__(self) -> None:
        for mode in (
            self.global_mode,
            self.account_mode,
            self.horizon_mode,
            self.strategy_mode,
        ):
            if mode is not None and not isinstance(mode, ApprovalMode):
                raise RiskPolicyError("Invalid approval mode.")
        for threshold in (
            self.threshold_notional_paise,
            self.threshold_risk_bps,
        ):
            if threshold is not None and (
                isinstance(threshold, bool)
                or not isinstance(threshold, int)
                or threshold < 0
            ):
                raise RiskPolicyError("Approval thresholds must be nonnegative.")

    def effective_mode(self) -> ApprovalMode:
        if self.strategy_mode is not None:
            return self.strategy_mode
        if self.horizon_mode is not None:
            return self.horizon_mode
        if self.account_mode is not None:
            return self.account_mode
        return self.global_mode


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_symbol_exposure_paise: int
    max_open_positions: int
    max_daily_loss_paise: int
    max_weekly_loss_paise: int
    max_monthly_loss_paise: int
    max_order_rate_per_minute: int

    def __post_init__(self) -> None:
        for value in (
            self.max_symbol_exposure_paise,
            self.max_open_positions,
            self.max_daily_loss_paise,
            self.max_weekly_loss_paise,
            self.max_monthly_loss_paise,
            self.max_order_rate_per_minute,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RiskPolicyError("Risk limits must be nonnegative integers.")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    state: RiskDecisionState
    vetoes: tuple[str, ...]


def evaluate_pretrade(
    limits: RiskLimits,
    *,
    symbol_exposure_paise: int,
    open_positions: int,
    daily_loss_paise: int,
    weekly_loss_paise: int,
    monthly_loss_paise: int,
    orders_this_minute: int,
    kill_switch: KillSwitchLevel,
    instrument_valid: bool,
    session_open: bool,
    stop_set: bool,
    data_usable: bool,
    available_cash_paise: int,
    order_notional_paise: int,
) -> RiskDecision:
    if not isinstance(limits, RiskLimits):
        raise RiskPolicyError("Risk limits are required.")
    for value in (
        symbol_exposure_paise,
        open_positions,
        daily_loss_paise,
        weekly_loss_paise,
        monthly_loss_paise,
        orders_this_minute,
        available_cash_paise,
        order_notional_paise,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RiskPolicyError("Pre-trade numeric values must be nonnegative.")
    if not isinstance(kill_switch, KillSwitchLevel):
        raise RiskPolicyError("Invalid kill switch level.")
    vetoes: list[str] = []
    if kill_switch is not KillSwitchLevel.NONE:
        vetoes.append("kill_switch")
    if not instrument_valid:
        vetoes.append("instrument_invalid")
    if not session_open:
        vetoes.append("session_closed")
    if not stop_set:
        vetoes.append("stop_missing")
    if not data_usable:
        vetoes.append("data_unusable")
    if symbol_exposure_paise + order_notional_paise > limits.max_symbol_exposure_paise:
        vetoes.append("symbol_exposure")
    if open_positions >= limits.max_open_positions:
        vetoes.append("open_position_limit")
    if daily_loss_paise > limits.max_daily_loss_paise:
        vetoes.append("daily_loss_limit")
    if weekly_loss_paise > limits.max_weekly_loss_paise:
        vetoes.append("weekly_loss_limit")
    if monthly_loss_paise > limits.max_monthly_loss_paise:
        vetoes.append("monthly_loss_limit")
    if orders_this_minute >= limits.max_order_rate_per_minute:
        vetoes.append("order_rate_limit")
    if order_notional_paise > available_cash_paise:
        vetoes.append("insufficient_cash")
    return RiskDecision(
        state=RiskDecisionState.BLOCK if vetoes else RiskDecisionState.ALLOW,
        vetoes=tuple(dict.fromkeys(vetoes)),
    )
