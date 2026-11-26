"""Market-open trigger engine and intraday paper fills for a campaign.

The campaign stores a breakout level, stop and target per symbol. This module is
the only place that watches those levels during the session:

* **Entries** — a 5-minute candle trades through the level inside the chase band
  → the existing deterministic risk gate runs, then a paper order is placed and
  filled at the candle's price.
* **Exits** — for campaign positions, the same candle's low/high against the
  stored stop/target. This closes the gap where ``paper.stops`` only sees daily
  bars at 15:20.
* **Idempotency** — every order and fill carries a campaign-scoped identifier, so
  a scan can run every five minutes, twice at once, or after a restart.

Prices come from the vendor router: Yahoo 5-minute candles (about 15 minutes
delayed) on a Kite-less install, Kite intraday when credentials are enabled. The
delay is reported in every result and is acceptable only because execution is
paper.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from bq.analysis.decision import TradePlanError, trade_plan_from_json
from bq.analysis.wiring import build_analysis_router
from bq.campaign import Campaign, CampaignItem, CampaignStore
from bq.campaign_config import CampaignConfig
from bq.core import (
    EXCHANGE_TIMEZONE,
    Clock,
    CurrencyPaise,
    Quantity,
    SystemClock,
    paise_from_price,
)
from bq.core import (
    exchange_time as core_exchange_time,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    utc as core_utc,
)
from bq.data.calendar import (
    ExchangeCalendar,
    SessionState,
    UncoveredCalendarDate,
)
from bq.data.symbols import SymbolError, resolve
from bq.data.vendors import VendorRouter
from bq.data.vendors.base import BarInterval
from bq.data.vendors.errors import VendorError
from bq.execution.context import ExecutionContextError, build_execution_context
from bq.execution.paper_executor import PaperExecutor, PaperExecutorError
from bq.logbook import record_decision
from bq.paper_trading import (
    PaperOrderType,
    PaperProduct,
    PaperSide,
    PaperTradingError,
    PaperTradingStore,
)
from bq.settings import SettingsStore

IST = EXCHANGE_TIMEZONE
SESSION_OPEN = time(9, 15)
ENTRY_WINDOW_END = time(15, 15)
MAX_CAMPAIGNS_PER_SCAN = 5
MAX_SCAN_NOTES = 50
MAX_REASON_CHARS = 200
DEFAULT_ORDER_RATE_PER_MINUTE = 12
DAILY_LOSS_BPS = 300
WEEKLY_LOSS_BPS = 600
MONTHLY_LOSS_BPS = 1000


class TriggerError(ValueError):
    """The trigger request is malformed."""


@dataclass(frozen=True, slots=True)
class _InstantClock:
    """Freeze the paper book at the scan instant.

    ``scan(as_of=...)`` is also used to rehearse a historical session. Writing
    fills or settlement checks against wall-clock time would then disagree with
    the session being processed, so every store opened inside a scan uses this
    clock instead.
    """

    instant: datetime

    def now(self) -> datetime:
        return self.instant


@dataclass(frozen=True, slots=True)
class TriggerOutcome:
    campaign_id: str
    symbol: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    as_of: datetime
    session_open: bool
    session_source: str
    campaigns: int
    entries_triggered: int
    entries_blocked: int
    exits: int
    expired: int
    outcomes: tuple[TriggerOutcome, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "session_open": self.session_open,
            "session_source": self.session_source,
            "campaigns": self.campaigns,
            "entries_triggered": self.entries_triggered,
            "entries_blocked": self.entries_blocked,
            "exits": self.exits,
            "expired": self.expired,
            "outcomes": [
                {
                    "campaign_id": item.campaign_id,
                    "symbol": item.symbol,
                    "status": item.status,
                    "detail": item.detail,
                }
                for item in self.outcomes
            ],
            "notes": list(self.notes),
        }


class TriggerEngine:
    def __init__(
        self,
        path: Path,
        settings: SettingsStore,
        *,
        clock: Clock | None = None,
        router: VendorRouter | None = None,
    ) -> None:
        self.path = path
        self.settings = settings
        self.clock = clock or SystemClock()
        self._router = router
        self.store = CampaignStore(path, self.clock)
        self.trading = PaperTradingStore(path, self.clock)

    @property
    def config(self) -> CampaignConfig:
        app = self.settings.load()
        configured = getattr(app, "campaign", None)
        if isinstance(configured, CampaignConfig):
            return configured
        return CampaignConfig.from_mapping(configured)

    def router(self) -> VendorRouter:
        if self._router is None:
            self._router = build_analysis_router(self.path, self.settings.load())
        return self._router

    def scan(self, *, as_of: datetime | None = None) -> ScanResult:
        config = self.config
        instant = core_utc(as_of or self.clock.now())
        local = core_exchange_time(instant)
        trade_date = local.date()
        open_now, source, notes = self._session_state(trade_date, instant)
        outcomes: list[TriggerOutcome] = []
        entries = blocked = exits = expired = 0

        if not config.enabled:
            return ScanResult(
                as_of=instant,
                session_open=open_now,
                session_source=source,
                campaigns=0,
                entries_triggered=0,
                entries_blocked=0,
                exits=0,
                expired=0,
                outcomes=(),
                notes=("campaign disabled",),
            )
        if not open_now:
            return ScanResult(
                as_of=instant,
                session_open=False,
                session_source=source,
                campaigns=0,
                entries_triggered=0,
                entries_blocked=0,
                exits=0,
                expired=0,
                outcomes=(),
                notes=tuple(notes),
            )

        fixed = _InstantClock(instant)
        self.store = CampaignStore(self.path, fixed)
        self.trading = PaperTradingStore(self.path, fixed)
        campaigns = self.store.active(limit=MAX_CAMPAIGNS_PER_SCAN)
        for campaign in campaigns:
            if campaign.trade_date > trade_date:
                continue
            for item in campaign.items:
                if item.status == "WATCHING":
                    if campaign.trade_date < trade_date:
                        self._expire(campaign, item, instant, "session_passed")
                        expired += 1
                        outcomes.append(
                            TriggerOutcome(
                                campaign.campaign_id,
                                item.symbol,
                                "EXPIRED",
                                "session passed without a trigger",
                            )
                        )
                        continue
                    result = self._try_entry(campaign, item, instant, config)
                    if result is None:
                        continue
                    outcomes.append(result)
                    if result.status == "FILLED":
                        entries += 1
                    elif result.status == "TRIGGERED":
                        entries += 1
                    elif result.status == "EXPIRED":
                        expired += 1
                    else:
                        blocked += 1
                elif item.status == "FILLED":
                    result = self._try_exit(campaign, item, instant, config)
                    if result is not None:
                        outcomes.append(result)
                        exits += 1
            self._refresh_campaign_status(campaign)

        for outcome in outcomes:
            record_decision(
                "trigger_outcome",
                campaign_id=outcome.campaign_id,
                symbol=outcome.symbol,
                status=outcome.status,
                detail=outcome.detail[:MAX_REASON_CHARS],
            )
        record_decision(
            "trigger_scan",
            as_of=instant.isoformat(),
            session_open=open_now,
            session_source=source,
            campaigns=len(campaigns),
            entries_triggered=entries,
            entries_blocked=blocked,
            exits=exits,
            expired=expired,
            notes=list(notes[:MAX_SCAN_NOTES]),
        )
        return ScanResult(
            as_of=instant,
            session_open=open_now,
            session_source=source,
            campaigns=len(campaigns),
            entries_triggered=entries,
            entries_blocked=blocked,
            exits=exits,
            expired=expired,
            outcomes=tuple(outcomes),
            notes=tuple(notes[:MAX_SCAN_NOTES]),
        )

    # -- entries ----------------------------------------------------------

    def _try_entry(
        self,
        campaign: Campaign,
        item: CampaignItem,
        now: datetime,
        config: CampaignConfig,
    ) -> TriggerOutcome | None:
        trigger = item.trigger
        if trigger is None or item.plan is None:
            return None
        valid_until = datetime.fromisoformat(str(trigger["valid_until"]))
        if now > valid_until:
            self._expire(campaign, item, now, "valid_until_passed")
            return TriggerOutcome(
                campaign.campaign_id, item.symbol, "EXPIRED", "valid_until passed"
            )
        bars = self._session_bars(item.symbol, campaign.trade_date, now)
        if bars is None:
            return None
        last = bars[-1]
        if now - last.event_at > timedelta(seconds=config.trigger_staleness_seconds):
            self._note(
                campaign,
                item,
                now,
                "stale_trigger_bar",
                {"age_seconds": int((now - last.event_at).total_seconds())},
            )
            return None

        level = int(trigger["level_paise"])
        if last.high < _price_paise(level):
            return None
        chase_limit = level + level * config.max_chase_bps // 10_000
        close_paise = paise_from_price(last.close).value
        open_paise = paise_from_price(last.open).value
        if open_paise > chase_limit or close_paise > chase_limit:
            self._set_status(
                campaign, item, "MISSED_CHASE", now, "gapped beyond the chase band"
            )
            return TriggerOutcome(
                campaign.campaign_id,
                item.symbol,
                "MISSED_CHASE",
                f"first touch above {chase_limit} paise",
            )

        entry = level if close_paise < level else min(close_paise, chase_limit)
        outcome = self._place_entry(campaign, item, entry, now, config)
        return outcome

    def _place_entry(
        self,
        campaign: Campaign,
        item: CampaignItem,
        entry_paise: int,
        now: datetime,
        config: CampaignConfig,
    ) -> TriggerOutcome | None:
        try:
            plan = trade_plan_from_json(item.plan)
        except TradePlanError as exc:
            self._set_status(campaign, item, "BLOCKED", now, f"bad plan: {exc}")
            return TriggerOutcome(
                campaign.campaign_id, item.symbol, "BLOCKED", "stored plan is invalid"
            )
        entry_price = Decimal(entry_paise) / Decimal(100)
        stop_price = plan.stop_loss
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            self._set_status(
                campaign, item, "BLOCKED", now, "entry is at or below the stop"
            )
            return TriggerOutcome(
                campaign.campaign_id,
                item.symbol,
                "BLOCKED",
                "entry is at or below the stop",
            )
        target_price = entry_price + risk_per_share * Decimal("1.5")
        quantity = self._size(entry_paise, paise_from_price(stop_price).value, config)
        if quantity <= 0:
            self._set_status(campaign, item, "BLOCKED", now, "size resolved to zero")
            return TriggerOutcome(
                campaign.campaign_id, item.symbol, "BLOCKED", "size resolved to zero"
            )
        try:
            plan = replace(
                plan,
                entry_price=entry_price,
                target_price=target_price,
                suggested_quantity=quantity,
            )
        except TradePlanError as exc:
            self._set_status(campaign, item, "BLOCKED", now, f"trigger geometry: {exc}")
            return TriggerOutcome(
                campaign.campaign_id,
                item.symbol,
                "BLOCKED",
                "entry crossed the stored stop or target",
            )
        try:
            snapshot = build_execution_context(
                self.path,
                plan,
                account_id=config.account_id,
                now=now,
                mark_price_paise=entry_paise,
                session_open_override=True,
            )
            executor = PaperExecutor(
                self.path, limits=self._risk_limits(config), clock=self.clock
            )
            execution = executor.execute(
                plan,
                context=snapshot.context,
                client_order_id=f"campaign-{campaign.campaign_id}-{item.symbol}",
                now=now,
                product=_paper_product(plan.product.value),
            )
        except (
            ExecutionContextError,
            PaperExecutorError,
            PaperTradingError,
            ValueError,
        ) as exc:
            self._set_status(
                campaign, item, "BLOCKED", now, _reason("gate_failed", exc)
            )
            return TriggerOutcome(
                campaign.campaign_id,
                item.symbol,
                "BLOCKED",
                _reason("gate_failed", exc),
            )

        if execution.order is None:
            vetoes = ",".join(execution.decision.vetoes) or "no order placed"
            self._set_status(campaign, item, "BLOCKED", now, vetoes)
            return TriggerOutcome(campaign.campaign_id, item.symbol, "BLOCKED", vetoes)

        order = execution.order
        if order.approval_status == "PENDING":
            self.store.update_item(
                campaign.campaign_id,
                item.symbol,
                status="TRIGGERED",
                outcome={
                    "state": "APPROVAL_PENDING",
                    "order_id": order.order_id,
                    "entry_paise": entry_paise,
                },
            )
            self.store.append_event(
                campaign.campaign_id,
                item.symbol,
                kind="approval_pending",
                detail={"order_id": order.order_id, "entry_paise": entry_paise},
                at=now,
            )
            return TriggerOutcome(
                campaign.campaign_id,
                item.symbol,
                "TRIGGERED",
                "order waits for owner approval",
            )

        try:
            _, fill = self.trading.fill(
                order.order_id,
                quantity=Quantity(quantity),
                price_paise=CurrencyPaise(entry_paise),
                idempotency_key=f"campaign-fill-{campaign.campaign_id}-{item.symbol}",
            )
        except PaperTradingError as exc:
            self._set_status(
                campaign, item, "BLOCKED", now, _reason("fill_failed", exc)
            )
            return TriggerOutcome(
                campaign.campaign_id,
                item.symbol,
                "BLOCKED",
                _reason("fill_failed", exc),
            )

        stop_paise = paise_from_price(plan.stop_loss).value
        target_paise = paise_from_price(plan.target_price).value
        self.store.update_item(
            campaign.campaign_id,
            item.symbol,
            status="FILLED",
            outcome={
                "state": "FILLED",
                "order_id": order.order_id,
                "fill_id": fill.fill_id,
                "entry_paise": entry_paise,
                "quantity": fill.quantity,
                "stop_paise": stop_paise,
                "target_paise": target_paise,
                "filled_at": fill.filled_at.isoformat(),
                "charges_paise": fill.charges_paise,
            },
        )
        self.store.append_event(
            campaign.campaign_id,
            item.symbol,
            kind="filled",
            detail={
                "entry_paise": entry_paise,
                "quantity": fill.quantity,
                "stop_paise": stop_paise,
                "target_paise": target_paise,
                "order_id": order.order_id,
            },
            at=now,
        )
        return TriggerOutcome(
            campaign.campaign_id,
            item.symbol,
            "FILLED",
            f"filled {fill.quantity} at {entry_paise} paise",
        )

    # -- exits ------------------------------------------------------------

    def _try_exit(
        self,
        campaign: Campaign,
        item: CampaignItem,
        now: datetime,
        config: CampaignConfig,
    ) -> TriggerOutcome | None:
        outcome = item.outcome or {}
        if outcome.get("state") == "CLOSED":
            return None
        stop_paise = int(outcome.get("stop_paise") or 0)
        target_paise = int(outcome.get("target_paise") or 0)
        if stop_paise <= 0 or target_paise <= 0:
            return None
        bars = self._session_bars(item.symbol, core_exchange_time(now).date(), now)
        if not bars:
            return None
        last = bars[-1]
        low = paise_from_price(last.low).value
        high = paise_from_price(last.high).value
        if low > stop_paise and high < target_paise:
            return None
        # Same-bar ambiguity is resolved conservatively: stop first.
        if low <= stop_paise:
            exit_paise = min(stop_paise, paise_from_price(last.open).value)
            reason = "stop"
        else:
            exit_paise = max(target_paise, paise_from_price(last.open).value)
            reason = "target"

        product = _product(item)
        held = self._held_quantity(config.account_id, _instrument(item), product)
        if held <= 0:
            self.store.update_item(
                campaign.campaign_id,
                item.symbol,
                status="CLOSED",
                outcome={**outcome, "state": "CLOSED", "exit_reason": "no_position"},
            )
            return TriggerOutcome(
                campaign.campaign_id, item.symbol, "CLOSED", "position already flat"
            )
        session_tag = core_exchange_time(now).strftime("%Y%m%d")
        client_order_id = (
            f"campaign-exit-{campaign.campaign_id}-{item.symbol}-{session_tag}"
        )
        self._cancel_stale_exits(
            config.account_id,
            f"campaign-exit-{campaign.campaign_id}-{item.symbol}-",
            client_order_id,
        )
        try:
            order = self.trading.place_order(
                account_id=config.account_id,
                instrument_id=_instrument(item),
                client_order_id=client_order_id,
                side=PaperSide.SELL,
                order_type=PaperOrderType.LIMIT,
                quantity=Quantity(held),
                limit_price_paise=CurrencyPaise(exit_paise),
                product=product,
            )
            _, fill = self.trading.fill(
                order.order_id,
                quantity=Quantity(held),
                price_paise=CurrencyPaise(exit_paise),
                idempotency_key=(
                    f"campaign-exit-fill-{campaign.campaign_id}-"
                    f"{item.symbol}-{session_tag}"
                ),
            )
        except PaperTradingError as exc:
            self._note(campaign, item, now, "exit_failed", {"error": _reason("", exc)})
            return TriggerOutcome(
                campaign.campaign_id,
                item.symbol,
                "FILLED",
                _reason("exit_failed", exc),
            )

        entry_paise = int(outcome.get("entry_paise") or 0)
        risk = max(1, entry_paise - stop_paise)
        r_multiple = (exit_paise - entry_paise) / risk
        self.store.update_item(
            campaign.campaign_id,
            item.symbol,
            status="CLOSED",
            outcome={
                **outcome,
                "state": "CLOSED",
                "exit_reason": reason,
                "exit_paise": exit_paise,
                "exit_at": fill.filled_at.isoformat(),
                "net_pnl_paise": fill.net_pnl_paise or 0,
            },
            r_multiple=r_multiple,
        )
        self.store.append_event(
            campaign.campaign_id,
            item.symbol,
            kind="exited",
            detail={
                "reason": reason,
                "exit_paise": exit_paise,
                "net_pnl_paise": fill.net_pnl_paise or 0,
                "r_multiple": round(r_multiple, 4),
            },
            at=now,
        )
        return TriggerOutcome(
            campaign.campaign_id,
            item.symbol,
            "CLOSED",
            f"{reason} exit at {exit_paise} paise ({r_multiple:+.2f}R)",
        )

    # -- helpers ----------------------------------------------------------

    def _session_bars(self, symbol: str, trade_date: date, now: datetime):
        start = datetime.combine(trade_date, SESSION_OPEN, tzinfo=IST)
        if now <= start:
            return None
        try:
            return (
                self.router()
                .bars(
                    resolve(symbol),
                    interval=BarInterval.MINUTE_5,
                    start=start,
                    end=now,
                )
                .data
            )
        except (SymbolError, VendorError, ValueError):
            return None

    def _session_state(
        self, trade_date: date, now: datetime
    ) -> tuple[bool, str, list[str]]:
        local = core_exchange_time(now)
        notes: list[str] = []
        config = self.config
        if trade_date.isoformat() in config.skip_dates:
            return False, "skip_date", ["Session is listed in campaign.skip_dates."]
        try:
            session = ExchangeCalendar(self.path).session_for(
                "NSE", trade_date, as_of=now
            )
        except UncoveredCalendarDate:
            notes.append(
                "Exchange calendar has no entry for this session; using weekday "
                "and 09:15-15:30 fallback."
            )
            if local.weekday() >= 5:
                return False, "weekend", notes
            inside = SESSION_OPEN <= local.time() <= time(15, 30)
            return inside, "weekday_fallback", notes
        if (
            session.state is SessionState.OPEN
            and session.opens_at is not None
            and session.closes_at is not None
        ):
            return session.opens_at <= now <= session.closes_at, "calendar", notes
        return False, session.state.value, notes

    def _risk_limits(self, config: CampaignConfig):
        from bq.risk.policy import RiskLimits

        configured = self.settings.load().risk_limits
        if configured is not None:
            return configured
        equity = max(1, config.equity_paise)
        return RiskLimits(
            max_symbol_exposure_paise=max(1, equity * 2 // 5),
            max_open_positions=config.max_open_positions,
            max_daily_loss_paise=max(1, equity * DAILY_LOSS_BPS // 10_000),
            max_weekly_loss_paise=max(1, equity * WEEKLY_LOSS_BPS // 10_000),
            max_monthly_loss_paise=max(1, equity * MONTHLY_LOSS_BPS // 10_000),
            max_order_rate_per_minute=DEFAULT_ORDER_RATE_PER_MINUTE,
        )

    def _size(self, entry_paise: int, stop_paise: int, config: CampaignConfig) -> int:
        risk_per_share = entry_paise - stop_paise
        if risk_per_share <= 0:
            return 0
        equity, cash = self._account_funds(config)
        limits = self._risk_limits(config)
        risk_budget = equity * config.risk_basis_points // 10_000
        quantity = risk_budget // risk_per_share
        quantity = min(quantity, cash // entry_paise)
        quantity = min(quantity, limits.max_symbol_exposure_paise // entry_paise)
        return max(0, quantity)

    def _account_funds(self, config: CampaignConfig) -> tuple[int, int]:
        try:
            balance = self.trading.accounts.get_balance(config.account_id)
        except Exception:
            return config.equity_paise, config.available_cash_paise
        if balance.balance_paise <= 0:
            return config.equity_paise, config.available_cash_paise
        equity = min(config.equity_paise, balance.balance_paise)
        cash = min(config.available_cash_paise, balance.balance_paise)
        return equity, cash

    def _cancel_stale_exits(
        self, account_id: str, prefix: str, keep_client_order_id: str
    ) -> None:
        with closing(core_open_connection(self.path, foreign_keys=True)) as connection:
            rows = connection.execute(
                "SELECT order_id, client_order_id FROM paper_orders "
                "WHERE account_id = ? AND side = 'SELL' AND status = 'PENDING' "
                "AND client_order_id LIKE ?",
                (account_id, f"{prefix}%"),
            ).fetchall()
        for order_id, client_order_id in rows:
            if str(client_order_id) == keep_client_order_id:
                continue
            try:
                self.trading.cancel(str(order_id))
            except PaperTradingError:
                continue

    def _held_quantity(
        self, account_id: str, instrument_id: str, product: PaperProduct
    ) -> int:
        with closing(core_open_connection(self.path, foreign_keys=True)) as connection:
            row = connection.execute(
                "SELECT quantity FROM paper_product_positions "
                "WHERE account_id = ? AND instrument_id = ? AND product = ?",
                (account_id, instrument_id, product.value),
            ).fetchone()
        return int(row[0]) if row else 0

    def _expire(
        self, campaign: Campaign, item: CampaignItem, now: datetime, reason: str
    ) -> None:
        self._set_status(campaign, item, "EXPIRED", now, reason)

    def _set_status(
        self,
        campaign: Campaign,
        item: CampaignItem,
        status: str,
        now: datetime,
        reason: str,
    ) -> None:
        self.store.update_item(campaign.campaign_id, item.symbol, status=status)
        self.store.append_event(
            campaign.campaign_id,
            item.symbol,
            kind=status.lower(),
            detail={"reason": reason[:MAX_REASON_CHARS]},
            at=now,
        )

    def _note(
        self,
        campaign: Campaign,
        item: CampaignItem,
        now: datetime,
        kind: str,
        detail: dict[str, object],
    ) -> None:
        self.store.append_event(
            campaign.campaign_id, item.symbol, kind=kind, detail=detail, at=now
        )

    def _refresh_campaign_status(self, campaign: Campaign) -> None:
        current = self.store.get(campaign.campaign_id)
        if current.status in {"CANCELLED", "CLOSED"}:
            return
        statuses = {item.status for item in current.items}
        if "FILLED" in statuses or "WATCHING" in statuses:
            target = "ACTIVE"
        elif "TRIGGERED" in statuses:
            target = "ACTIVE"
        else:
            target = "CLOSED"
        if target != current.status:
            self.store.set_status(current.campaign_id, target)


def _price_paise(paise: int) -> Decimal:
    return Decimal(paise) / Decimal(100)


def _reason(prefix: str, exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"[:MAX_REASON_CHARS]
    return f"{prefix} ({message})" if prefix else message


def trigger_quantity(item: CampaignItem) -> int:
    trigger = item.trigger or {}
    if item.plan is None:
        return 0
    return int(trigger.get("quantity") or item.plan.get("suggested_quantity") or 0)


def _paper_product(product: str) -> PaperProduct:
    """Map the plan vocabulary (CNC/MIS) onto the paper book (CAS/MIS)."""
    return PaperProduct.CAS if product == "CNC" else PaperProduct.MIS


def _instrument(item: CampaignItem) -> str:
    """The paper book keys instruments by the canonical plan symbol."""
    if item.plan is None:
        return item.symbol
    return str(item.plan.get("symbol") or item.symbol)


def _product(item: CampaignItem) -> PaperProduct:
    if item.plan is None:
        return PaperProduct.CAS
    return _paper_product(str(item.plan.get("product", "CNC")))


__all__ = [
    "ENTRY_WINDOW_END",
    "ScanResult",
    "TriggerEngine",
    "TriggerError",
    "TriggerOutcome",
    "trigger_quantity",
]
