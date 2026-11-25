"""Deterministic paper execution for a validated trade plan.

This is the Phase 4 connector the plan deferred: it feeds a
:class:`bq.analysis.decision.TradePlan` through the existing deterministic
pre-trade gate (:func:`bq.risk.policy.evaluate_pretrade`) and, only when the gate
returns ``ALLOW``, places a **paper** order through
:class:`bq.paper_trading.PaperTradingStore`.

Nothing here touches a broker. ADR-001 stands: the "execution" is a paper order
with an exact-paise ledger, and a model or a plan cannot override the gate.

Three things are decided here, not elsewhere:

1. **Data freshness maps to the gate's ``data_usable`` flag.** ADR-004 says
   delayed data is ``WARN``, not ``BLOCK``, while critically stale data blocks.
   So a ``delayed_yfinance`` plan is *usable for paper* (it is labelled a
   warning), and only a critically stale price blocks. That is the honest
   reading for a paper system: there is no live order to protect. A future live
   path must re-evaluate this mapping.
2. **The plan quantity is the only quantity.** The executor re-checks the plan's
   own sizer arithmetic is not bypassed by placing exactly
   ``plan.suggested_quantity`` and no other figure.
3. **Only long LIMIT plans are accepted.** There is no short-margin model and no
   market-order fill model, so the executor refuses anything else rather than
   pretending to fill it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from bq.analysis.decision import DataFreshness, Signal, TradePlan
from bq.core import Clock, CurrencyPaise, Quantity, SystemClock
from bq.paper_accounts import PaperAccountError
from bq.paper_trading import (
    PaperOrder,
    PaperOrderType,
    PaperSide,
    PaperTradingError,
    PaperTradingStore,
)
from bq.risk.policy import (
    KillSwitchLevel,
    RiskDecision,
    RiskDecisionState,
    RiskLimits,
    evaluate_pretrade,
)

# Unvalidated freshness defaults. A daily swing plan may lag the newest settled
# session by up to two weekday sessions; a 15m intraday plan by fifteen minutes.
# These are starting points, not a verified exchange rule.
SWING_MAX_STALE_WEEKDAYS = 2
INTRADAY_CRITICAL_AFTER_SECONDS = 15 * 60

# NSE/BSE continuous session: 09:15–15:30 IST, i.e. 6h15m. A daily bar is stamped
# at the session *open* but is only settled at the *close*, so age measured from
# the open overstates staleness by this much for every daily bar.
SESSION_LENGTH = timedelta(hours=6, minutes=15)


class PaperExecutorError(ValueError):
    """A plan could not become a paper order. The reason is in the message."""


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Account and portfolio state the gate needs, supplied by the caller.

    Every figure is an integer in paise or a count. The executor does not read
    the account itself; the caller owns the snapshot, which keeps the executor a
    pure function of its inputs and makes the gate's arithmetic trivially
    testable.
    """

    account_id: str
    available_cash_paise: int
    open_positions: int
    symbol_exposure_paise: int
    daily_loss_paise: int
    weekly_loss_paise: int
    monthly_loss_paise: int
    orders_this_minute: int
    kill_switch: KillSwitchLevel
    instrument_valid: bool
    session_open: bool

    def __post_init__(self) -> None:
        for label, value in (
            ("available_cash", self.available_cash_paise),
            ("open_positions", self.open_positions),
            ("symbol_exposure", self.symbol_exposure_paise),
            ("daily_loss", self.daily_loss_paise),
            ("weekly_loss", self.weekly_loss_paise),
            ("monthly_loss", self.monthly_loss_paise),
            ("orders_this_minute", self.orders_this_minute),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PaperExecutorError(f"{label} must be a nonnegative integer.")
        if not isinstance(self.kill_switch, KillSwitchLevel):
            raise PaperExecutorError("kill_switch must be a KillSwitchLevel.")
        for label, value in (
            ("instrument_valid", self.instrument_valid),
            ("session_open", self.session_open),
        ):
            if not isinstance(value, bool):
                raise PaperExecutorError(f"{label} must be a boolean.")


@dataclass(frozen=True, slots=True)
class FreshnessAssessment:
    """What the gate gets for ``data_usable``, plus the label for the record."""

    usable: bool
    label: str
    age_seconds: float


@dataclass(frozen=True, slots=True)
class PaperExecution:
    """The result of executing a plan: the gate decision and any paper order."""

    decision: RiskDecision
    freshness: FreshnessAssessment
    order: PaperOrder | None


def _weekdays_between(start: datetime, end: datetime) -> int:
    """Weekday boundaries crossed, ignoring weekends.

    A calendar-seconds threshold blocks every Monday morning, because a Friday
    close is then two weekend days "old" while no new session has traded. Counting
    weekday boundaries is the honest measure of how many sessions a plan has
    missed. Public holidays are not modelled — see the note in ``assess_freshness``.
    """
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor.date() < end.date():
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def assess_freshness(
    plan: TradePlan,
    *,
    now: datetime,
    critical_after_seconds: int | None = None,
    max_stale_weekdays: int | None = None,
) -> FreshnessAssessment:
    """Map a plan's declared freshness and age to a gate input.

    ``realtime_kite`` is usable while fresh and blocked once critically stale.
    ``delayed_yfinance`` is usable but labelled a warning, because ADR-004 treats
    delay as ``WARN`` rather than ``BLOCK`` — for paper, a delayed price is still
    a valid simulation input.

    **Two different clocks, deliberately.**

    * Intraday is measured in **wall-clock seconds** from the bar's own stamp. A
      15-minute candle really does lose value in 15 minutes.
    * Swing is measured in **weekday sessions missed**, because a daily bar is
      only settled at the close and no session trades at the weekend. A calendar
      threshold would block every Monday morning for a plan built on Friday's
      close, which is the freshest data that can exist at that moment.

    Public holidays are **not** modelled: a holiday week counts as one extra
    missed session. That errs toward blocking, which is the safe direction, and
    the trading calendar already exists to close the gap later.

    ``critical_after_seconds`` / ``max_stale_weekdays`` override the defaults for
    one call; tests use them to avoid depending on today's date.
    """
    if not isinstance(plan, TradePlan):
        raise PaperExecutorError("A TradePlan is required.")
    if now.utcoffset() is None:
        raise PaperExecutorError("now must be timezone-aware.")
    moment = now.astimezone(UTC)
    settled_at = plan.as_of.astimezone(UTC)

    if plan.timeframe == "intraday":
        threshold = (
            critical_after_seconds
            if critical_after_seconds is not None
            else INTRADAY_CRITICAL_AFTER_SECONDS
        )
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, int)
            or threshold < 0
        ):
            raise PaperExecutorError("critical_after_seconds must be nonnegative.")
        age = max(0.0, (moment - settled_at).total_seconds())
        stale = age > threshold
    else:
        # A daily bar settles at the close, not the open.
        settled_at += SESSION_LENGTH
        budget = (
            max_stale_weekdays
            if max_stale_weekdays is not None
            else SWING_MAX_STALE_WEEKDAYS
        )
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise PaperExecutorError("max_stale_weekdays must be nonnegative.")
        age = max(0.0, (moment - settled_at).total_seconds())
        stale = _weekdays_between(settled_at, moment) > budget

    if stale:
        return FreshnessAssessment(usable=False, label="stale_block", age_seconds=age)
    if plan.data_freshness is DataFreshness.REALTIME_KITE:
        return FreshnessAssessment(usable=True, label="realtime", age_seconds=age)
    return FreshnessAssessment(usable=True, label="delayed_warn", age_seconds=age)


def _price_paise(price: Decimal) -> CurrencyPaise:
    return CurrencyPaise(
        int((price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    )


class PaperExecutor:
    """Runs the deterministic gate and places a paper order on ``ALLOW``."""

    def __init__(
        self,
        path: Path,
        *,
        limits: RiskLimits,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(limits, RiskLimits):
            raise PaperExecutorError("RiskLimits are required.")
        self.limits = limits
        self.store = PaperTradingStore(path, clock)
        self.clock = clock or SystemClock()

    def execute(
        self,
        plan: TradePlan,
        *,
        context: ExecutionContext,
        client_order_id: str,
        now: datetime | None = None,
    ) -> PaperExecution:
        """Gate a plan, then place a paper order only if it is allowed."""
        if not isinstance(plan, TradePlan):
            raise PaperExecutorError("A TradePlan is required.")
        if not isinstance(context, ExecutionContext):
            raise PaperExecutorError("An ExecutionContext is required.")
        if plan.signal is not Signal.BUY:
            raise PaperExecutorError(
                "The paper executor only accepts BUY plans; there is no short model."
            )
        if plan.signal is Signal.BUY and plan.order_type.value != "LIMIT":
            # The plan contract already forbids non-LIMIT, so this is defensive.
            raise PaperExecutorError("Only LIMIT plans can be executed.")

        instant = (now or self.clock.now()).astimezone(UTC)
        freshness = assess_freshness(plan, now=instant)
        decision = evaluate_pretrade(
            self.limits,
            symbol_exposure_paise=context.symbol_exposure_paise,
            open_positions=context.open_positions,
            daily_loss_paise=context.daily_loss_paise,
            weekly_loss_paise=context.weekly_loss_paise,
            monthly_loss_paise=context.monthly_loss_paise,
            orders_this_minute=context.orders_this_minute,
            kill_switch=context.kill_switch,
            instrument_valid=context.instrument_valid,
            session_open=context.session_open,
            stop_set=True,  # the plan contract guarantees a stop
            data_usable=freshness.usable,
            available_cash_paise=context.available_cash_paise,
            order_notional_paise=plan.notional_paise().value,
        )
        if decision.state is not RiskDecisionState.ALLOW:
            return PaperExecution(decision=decision, freshness=freshness, order=None)
        try:
            order = self.store.place_order(
                account_id=context.account_id,
                instrument_id=plan.symbol,
                client_order_id=client_order_id,
                side=PaperSide.BUY,
                order_type=PaperOrderType.LIMIT,
                quantity=Quantity(plan.suggested_quantity),
                limit_price_paise=_price_paise(plan.entry_price),
            )
        except (PaperTradingError, PaperAccountError) as exc:
            raise PaperExecutorError(str(exc)) from exc
        return PaperExecution(decision=decision, freshness=freshness, order=order)


__all__ = [
    "ExecutionContext",
    "FreshnessAssessment",
    "INTRADAY_CRITICAL_AFTER_SECONDS",
    "PaperExecution",
    "PaperExecutor",
    "PaperExecutorError",
    "SESSION_LENGTH",
    "SWING_MAX_STALE_WEEKDAYS",
    "assess_freshness",
]
