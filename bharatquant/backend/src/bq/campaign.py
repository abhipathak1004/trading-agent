"""The daily catalyst campaign: news → shortlist → plans → trigger levels.

This module is the deterministic glue the rest of the system was missing. It
does not re-implement technical analysis, sizing, risk or paper execution; it
calls the modules that already own those, and stores the campaign as a reviewable
record.

Cost design
-----------
The default ``analysis_depth="deterministic"`` runs the indicator/rules half of
:class:`AnalysisPipeline` (no debate, no risk model, no final-gate model). Eight
names therefore cost zero LLM tokens. ``analysis_depth="full"`` opts into the
same wired pipeline the Analysis tab uses.

Safety design
-------------
* Plans are frozen per campaign row and never rewritten by a later stage.
* A regeneration is refused once any item has progressed past ``WATCHING``.
* All prices and levels are integer paise; no model computes them.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from bq.analysis.builder import build_analysis_pipeline, build_deterministic_pipeline
from bq.analysis.pipeline import AnalysisResult, PipelineError
from bq.analysis.run_settings import AnalysisRunError, AnalysisRunStore
from bq.analysis.wiring import build_analysis_router
from bq.campaign_config import CampaignConfig
from bq.campaign_feedback import campaign_review, event_weights
from bq.catalysts import (
    AliasIndex,
    CatalystStore,
    extract_many,
)
from bq.confidence import ConfidenceDisposition
from bq.core import (
    EXCHANGE_TIMEZONE,
    Clock,
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
    parse_timestamp as core_parse_timestamp,
)
from bq.core import (
    utc as core_utc,
)
from bq.data.equity_master import EquityMasterStore, listing_date
from bq.data.symbols import SymbolError, resolve
from bq.data.vendors import VendorRouter
from bq.data.vendors.base import BarInterval
from bq.infra.equity_listing import fetch_listing
from bq.infra.news_feed import fetch_feed
from bq.news import (
    MAX_ITEMS_PER_INGEST,
    NewsError,
    NewsStore,
    parse_feed,
)
from bq.paper_accounts import PaperAccountError, PaperAccountStore
from bq.settings import SettingsStore
from bq.shortlist import (
    MAX_SHORTLIST,
    Liquidity,
    ShortlistConfig,
    build_shortlist,
)

CAMPAIGN_PREFIX = "camp_"
SESSION_CUTOFF = time(15, 30)
MIN_REWARD_RISK_BPS = 15_000
MAX_CAMPAIGN_ITEMS = 10
CAMPAIGN_STATUSES = frozenset(
    {"RUNNING", "READY", "ACTIVE", "CLOSED", "CANCELLED", "FAILED"}
)
ITEM_STATUSES = frozenset(
    {
        "WATCHING",
        "TRIGGERED",
        "FILLED",
        "MISSED_CHASE",
        "INVALIDATED",
        "EXPIRED",
        "BLOCKED",
        "CANCELLED",
        "CLOSED",
    }
)
BLOCKING_VETOES = frozenset({"no_deterministic_long_case", "no_valid_stop_distance"})


class CampaignError(ValueError):
    """The campaign request is invalid or the campaign is in a terminal state."""


class CampaignNotFound(CampaignError):
    """No campaign exists for the supplied identifier or date."""


def _timestamp(value: datetime) -> str:
    return core_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return core_parse_timestamp(value)


def next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def campaign_trade_date(as_of: datetime) -> date:
    """The session the campaign is for.

    Research after the close (or on a weekend) targets the next weekday. Before
    the close it targets today, so an intraday re-run still has a valid session.
    """
    local = core_exchange_time(as_of)
    if local.weekday() >= 5:
        return next_weekday(local.date())
    if local.time() >= SESSION_CUTOFF:
        return next_weekday(local.date())
    return local.date()


def _valid_until(trade_date: date, clock_value: time) -> datetime:
    return datetime.combine(trade_date, clock_value, tzinfo=EXCHANGE_TIMEZONE)


@dataclass(frozen=True, slots=True)
class CampaignItem:
    campaign_id: str
    symbol: str
    rank: int
    catalyst_score: float
    events: tuple[dict[str, object], ...]
    analysis_run_id: str | None
    verdict: str
    plan: dict[str, object] | None
    trigger: dict[str, object] | None
    status: str
    outcome: dict[str, object] | None
    r_multiple: float | None
    updated_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "rank": self.rank,
            "catalyst_score": self.catalyst_score,
            "events": list(self.events),
            "analysis_run_id": self.analysis_run_id,
            "verdict": self.verdict,
            "plan": self.plan,
            "trigger": self.trigger,
            "status": self.status,
            "outcome": self.outcome,
            "r_multiple": self.r_multiple,
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Campaign:
    campaign_id: str
    trade_date: date
    created_at: datetime
    as_of: datetime
    config: dict[str, object]
    status: str
    summary: dict[str, object]
    items: tuple[CampaignItem, ...]

    @property
    def watching(self) -> tuple[CampaignItem, ...]:
        return tuple(item for item in self.items if item.status == "WATCHING")

    def to_dict(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "trade_date": self.trade_date.isoformat(),
            "created_at": self.created_at.isoformat(),
            "as_of": self.as_of.isoformat(),
            "config": self.config,
            "status": self.status,
            "summary": self.summary,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class _FixedClock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


@dataclass(frozen=True, slots=True)
class _BarsContext:
    last_close_paise: int
    avg_volume_20d: int
    previous_high_paise: int
    previous_close_paise: int


class CampaignStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=True)

    def upsert_campaign(
        self,
        *,
        campaign_id: str,
        trade_date: date,
        as_of: datetime,
        config: Mapping[str, object],
        status: str,
        summary: Mapping[str, object],
    ) -> None:
        if status not in CAMPAIGN_STATUSES:
            raise CampaignError(f"Unknown campaign status: {status}.")
        now = core_utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO campaigns "
                    "(campaign_id, trade_date, created_at, as_of, config_json, "
                    "status, summary_json) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (campaign_id) DO UPDATE SET "
                    "as_of = excluded.as_of, config_json = excluded.config_json, "
                    "status = excluded.status, summary_json = excluded.summary_json",
                    (
                        campaign_id,
                        trade_date.isoformat(),
                        _timestamp(now),
                        _timestamp(as_of),
                        json.dumps(config, sort_keys=True),
                        status,
                        json.dumps(summary, sort_keys=True),
                    ),
                )

    def set_status(self, campaign_id: str, status: str) -> None:
        if status not in CAMPAIGN_STATUSES:
            raise CampaignError(f"Unknown campaign status: {status}.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "UPDATE campaigns SET status = ? WHERE campaign_id = ?",
                    (status, campaign_id),
                )

    def set_summary(self, campaign_id: str, summary: Mapping[str, object]) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "UPDATE campaigns SET summary_json = ? WHERE campaign_id = ?",
                    (json.dumps(summary, sort_keys=True), campaign_id),
                )

    def upsert_item(
        self,
        *,
        campaign_id: str,
        symbol: str,
        rank: int,
        catalyst_score: float,
        events: Sequence[Mapping[str, object]],
        verdict: str,
        status: str,
        analysis_run_id: str | None = None,
        plan: Mapping[str, object] | None = None,
        trigger: Mapping[str, object] | None = None,
    ) -> None:
        if status not in ITEM_STATUSES:
            raise CampaignError(f"Unknown campaign item status: {status}.")
        now = _timestamp(core_utc(self.clock.now()))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO campaign_items "
                    "(campaign_id, symbol, rank, catalyst_score, events_json, "
                    "analysis_run_id, verdict, plan_json, trigger_json, status, "
                    "outcome_json, r_multiple, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?) "
                    "ON CONFLICT (campaign_id, symbol) DO UPDATE SET "
                    "rank = excluded.rank, "
                    "catalyst_score = excluded.catalyst_score, "
                    "events_json = excluded.events_json, "
                    "analysis_run_id = excluded.analysis_run_id, "
                    "verdict = excluded.verdict, "
                    "plan_json = excluded.plan_json, "
                    "trigger_json = excluded.trigger_json, "
                    "status = excluded.status, "
                    "outcome_json = NULL, r_multiple = NULL, "
                    "updated_at = excluded.updated_at",
                    (
                        campaign_id,
                        symbol,
                        rank,
                        catalyst_score,
                        json.dumps(list(events), sort_keys=True),
                        analysis_run_id,
                        verdict,
                        None if plan is None else json.dumps(plan, sort_keys=True),
                        None
                        if trigger is None
                        else json.dumps(trigger, sort_keys=True),
                        status,
                        now,
                    ),
                )

    def update_item(
        self,
        campaign_id: str,
        symbol: str,
        *,
        status: str | None = None,
        verdict: str | None = None,
        analysis_run_id: str | None = None,
        trigger: Mapping[str, object] | None = None,
        outcome: Mapping[str, object] | None = None,
        r_multiple: float | None = None,
        clear_plan: bool = False,
    ) -> None:
        assignments: list[str] = []
        params: list[object] = []
        if status is not None:
            if status not in ITEM_STATUSES:
                raise CampaignError(f"Unknown campaign item status: {status}.")
            assignments.append("status = ?")
            params.append(status)
        if verdict is not None:
            assignments.append("verdict = ?")
            params.append(verdict)
        if analysis_run_id is not None:
            assignments.append("analysis_run_id = ?")
            params.append(analysis_run_id)
        if trigger is not None:
            assignments.append("trigger_json = ?")
            params.append(json.dumps(trigger, sort_keys=True))
        if outcome is not None:
            assignments.append("outcome_json = ?")
            params.append(json.dumps(outcome, sort_keys=True))
        if r_multiple is not None:
            assignments.append("r_multiple = ?")
            params.append(r_multiple)
        if clear_plan:
            assignments.extend(["plan_json = NULL", "trigger_json = NULL"])
        if not assignments:
            return
        assignments.append("updated_at = ?")
        params.append(_timestamp(core_utc(self.clock.now())))
        params.extend([campaign_id, symbol])
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "UPDATE campaign_items SET "
                    + ", ".join(assignments)
                    + " WHERE campaign_id = ? AND symbol = ?",
                    params,
                )

    def append_event(
        self,
        campaign_id: str,
        symbol: str,
        *,
        kind: str,
        detail: Mapping[str, object],
        at: datetime | None = None,
    ) -> None:
        occurred = core_utc(at or self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO campaign_item_events "
                    "(event_id, campaign_id, symbol, at, kind, detail_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        secrets.token_urlsafe(18),
                        campaign_id,
                        symbol,
                        _timestamp(occurred),
                        kind,
                        json.dumps(detail, sort_keys=True),
                    ),
                )

    def events(
        self, campaign_id: str, *, symbol: str | None = None, limit: int = 200
    ) -> tuple[dict[str, object], ...]:
        query = (
            "SELECT campaign_id, symbol, at, kind, detail_json "
            "FROM campaign_item_events WHERE campaign_id = ?"
        )
        params: list[object] = [campaign_id]
        if symbol is not None:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY at, rowid LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            {
                "campaign_id": str(row[0]),
                "symbol": str(row[1]),
                "at": _parse(str(row[2])).isoformat(),
                "kind": str(row[3]),
                "detail": json.loads(str(row[4])),
            }
            for row in rows
        )

    def get(self, campaign_id: str) -> Campaign:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT campaign_id, trade_date, created_at, as_of, config_json, "
                "status, summary_json FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            if row is None:
                raise CampaignNotFound(f"Campaign '{campaign_id}' was not found.")
            items = self._items(connection, campaign_id)
        return self._record(row, items)

    def find_by_date(self, trade_date: date) -> Campaign | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT campaign_id, trade_date, created_at, as_of, config_json, "
                "status, summary_json FROM campaigns WHERE trade_date = ?",
                (trade_date.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            items = self._items(connection, str(row[0]))
        return self._record(row, items)

    def active(self, *, limit: int = 5) -> tuple[Campaign, ...]:
        """Campaigns that may still have something to watch or manage."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT campaign_id FROM campaigns "
                "WHERE status IN ('ACTIVE', 'READY') "
                "ORDER BY trade_date DESC, created_at DESC LIMIT ?",
                (max(1, min(limit, 20)),),
            ).fetchall()
            campaigns = []
            for row in rows:
                campaign_id = str(row[0])
                record = connection.execute(
                    "SELECT campaign_id, trade_date, created_at, as_of, "
                    "config_json, status, summary_json FROM campaigns "
                    "WHERE campaign_id = ?",
                    (campaign_id,),
                ).fetchone()
                if record is not None:
                    campaigns.append(
                        self._record(record, self._items(connection, campaign_id))
                    )
        return tuple(campaigns)

    def latest(self) -> Campaign | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT campaign_id, trade_date, created_at, as_of, config_json, "
                "status, summary_json FROM campaigns "
                "ORDER BY trade_date DESC, created_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            items = self._items(connection, str(row[0]))
        return self._record(row, items)

    def _items(
        self, connection: sqlite3.Connection, campaign_id: str
    ) -> tuple[CampaignItem, ...]:
        rows = connection.execute(
            "SELECT campaign_id, symbol, rank, catalyst_score, events_json, "
            "analysis_run_id, verdict, plan_json, trigger_json, status, "
            "outcome_json, r_multiple, updated_at FROM campaign_items "
            "WHERE campaign_id = ? ORDER BY rank, symbol",
            (campaign_id,),
        ).fetchall()
        return tuple(
            CampaignItem(
                campaign_id=str(row[0]),
                symbol=str(row[1]),
                rank=int(row[2]),
                catalyst_score=float(row[3]),
                events=tuple(json.loads(str(row[4]))),
                analysis_run_id=None if row[5] is None else str(row[5]),
                verdict=str(row[6]),
                plan=None if row[7] is None else json.loads(str(row[7])),
                trigger=None if row[8] is None else json.loads(str(row[8])),
                status=str(row[9]),
                outcome=None if row[10] is None else json.loads(str(row[10])),
                r_multiple=None if row[11] is None else float(row[11]),
                updated_at=_parse(str(row[12])),
            )
            for row in rows
        )

    def _record(
        self, row: Sequence[object], items: tuple[CampaignItem, ...]
    ) -> Campaign:
        return Campaign(
            campaign_id=str(row[0]),
            trade_date=date.fromisoformat(str(row[1])),
            created_at=_parse(str(row[2])),
            as_of=_parse(str(row[3])),
            config=json.loads(str(row[4])),
            status=str(row[5]),
            summary=json.loads(str(row[6])),
            items=items,
        )


class CampaignService:
    def __init__(
        self,
        path: Path,
        settings: SettingsStore,
        *,
        clock: Clock | None = None,
        router: VendorRouter | None = None,
        news_fetcher: Callable[[str], bytes] | None = None,
        listing_fetcher: Callable[[], bytes] | None = None,
    ) -> None:
        self.path = path
        self.settings = settings
        self.clock = clock or SystemClock()
        self._router = router
        self._news_fetcher = news_fetcher or fetch_feed
        self._listing_fetcher = listing_fetcher or fetch_listing
        self.store = CampaignStore(path, self.clock)

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

    def refresh_master(self, *, as_of: datetime | None = None) -> int:
        instant = core_utc(as_of or self.clock.now())
        payload = self._listing_fetcher()
        return EquityMasterStore(self.path, self.clock).refresh(
            payload, as_of=listing_date(instant)
        )

    def research(
        self,
        *,
        as_of: datetime | None = None,
        force: bool = False,
        top_n: int | None = None,
    ) -> Campaign:
        config = self.config
        if not config.enabled:
            raise CampaignError("The campaign is disabled in settings.")
        instant = core_utc(as_of or self.clock.now())
        trade_date = campaign_trade_date(instant)
        campaign_id = f"{CAMPAIGN_PREFIX}{trade_date.strftime('%Y%m%d')}"

        existing = self.store.find_by_date(trade_date)
        if existing is not None and not force:
            return existing
        if existing is not None and any(
            item.status not in {"WATCHING", "BLOCKED", "CANCELLED"}
            for item in existing.items
        ):
            raise CampaignError(
                "This campaign has already progressed past watching; cancel or "
                "start the next day instead of regenerating it."
            )

        master = EquityMasterStore(self.path, self.clock)
        if master.count() == 0:
            self.refresh_master(as_of=instant)
        if master.count() == 0:
            raise CampaignError(
                "The equity master is empty; run `bq campaign refresh-master` first."
            )

        news_store = NewsStore(self.path, _FixedClock(instant))
        news_summary = self._ensure_news(news_store, instant, config)
        cutoff = instant - timedelta(hours=config.news_lookback_hours)
        items = tuple(
            item
            for item in news_store.recent(limit=500, as_of=instant)
            if item.published_at >= cutoff
        )
        index = AliasIndex.from_aliases(master.aliases(as_of=trade_date))
        catalysts = extract_many(items, index, trade_date=trade_date)
        catalyst_store = CatalystStore(self.path, self.clock)
        recorded = catalyst_store.record(catalysts, trade_date=trade_date)
        fresh = {(item.news_id, item.symbol, item.event) for item in catalysts}
        stored = tuple(
            item
            for item in catalyst_store.for_date(trade_date)
            if (item.news_id, item.symbol, item.event) in fresh
        )
        unmatched = catalyst_store.unmatched_corporate_count(
            trade_date, start=cutoff, end=instant
        )
        weights = event_weights(self.path)

        shortlist_config = ShortlistConfig(
            top_n=MAX_SHORTLIST,
            min_catalyst_strength=config.min_catalyst_strength,
            min_price_paise=config.min_price_paise,
            min_avg_volume=config.min_avg_volume,
        )
        preliminary = build_shortlist(
            stored,
            trade_date=trade_date,
            config=shortlist_config,
            event_weights=weights,
            unmatched_corporate=unmatched,
        )
        bars = self._bars_context(
            [item.symbol for item in preliminary.items], trade_date, instant
        )
        liquidity = {
            symbol: Liquidity(
                last_close_paise=context.last_close_paise,
                avg_volume_20d=context.avg_volume_20d,
            )
            for symbol, context in bars.items()
        }
        final_config = ShortlistConfig(
            top_n=min(top_n or config.top_n, MAX_CAMPAIGN_ITEMS),
            min_catalyst_strength=config.min_catalyst_strength,
            min_price_paise=config.min_price_paise,
            min_avg_volume=config.min_avg_volume,
        )
        shortlist = build_shortlist(
            stored,
            trade_date=trade_date,
            config=final_config,
            event_weights=weights,
            liquidity=liquidity,
            unmatched_corporate=unmatched,
        )

        summary: dict[str, object] = {
            "master_as_of": (
                None
                if master.latest_as_of() is None
                else master.latest_as_of().isoformat()
            ),
            "news": news_summary,
            "news_items_scanned": len(items),
            "catalysts_extracted": len(catalysts),
            "catalysts_recorded": recorded,
            "catalysts_stored": len(stored),
            "unmatched_corporate": unmatched,
            "shortlist": shortlist.to_dict(),
            "analysis_depth": config.analysis_depth,
            "account_id": config.account_id,
            "notes": [],
        }
        self.store.upsert_campaign(
            campaign_id=campaign_id,
            trade_date=trade_date,
            as_of=instant,
            config=config.to_dict(),
            status="RUNNING",
            summary=summary,
        )

        def cancel_stale(symbols: set[str]) -> None:
            if existing is None:
                return
            for old in existing.items:
                if old.symbol in symbols or old.status in {"CANCELLED", "CLOSED"}:
                    continue
                self.store.update_item(
                    campaign_id,
                    old.symbol,
                    status="CANCELLED",
                    clear_plan=True,
                )
                self.store.append_event(
                    campaign_id,
                    old.symbol,
                    kind="cancelled",
                    detail={"reason": "regenerated"},
                    at=instant,
                )

        if not shortlist.items:
            cancel_stale(set())
            summary["notes"] = ["No qualifying positive catalyst for this session."]
            self.store.set_summary(campaign_id, summary)
            self.store.set_status(campaign_id, "READY")
            return self.store.get(campaign_id)

        equity, cash = self._equity_and_cash(config)
        open_positions = self._open_position_count(config.account_id)
        pipeline = (
            build_analysis_pipeline(self.path, self.settings.load(), self.settings)
            if config.analysis_depth == "full"
            else build_deterministic_pipeline(self.router(), clock=self.clock)
        )
        candidates = 0
        blocked = 0
        errors = 0
        for rank, item in enumerate(shortlist.items, start=1):
            context = bars.get(item.symbol)
            events = [
                {
                    "news_id": catalyst.news_id,
                    "event": catalyst.event.value,
                    "polarity": catalyst.polarity,
                    "strength": catalyst.strength,
                    "title": catalyst.title,
                    "published_at": catalyst.published_at.isoformat(),
                    "matched_terms": list(catalyst.matched_terms),
                }
                for catalyst in item.catalysts
            ]
            try:
                result = pipeline.run(
                    symbol=resolve(item.symbol),
                    timeframe="swing",
                    equity_paise=equity,
                    available_cash_paise=cash,
                    risk_basis_points=config.risk_basis_points,
                    as_of=instant,
                )
            except (PipelineError, SymbolError) as exc:
                errors += 1
                self.store.upsert_item(
                    campaign_id=campaign_id,
                    symbol=item.symbol,
                    rank=rank,
                    catalyst_score=item.score,
                    events=events,
                    verdict="ERROR",
                    status="BLOCKED",
                )
                self.store.append_event(
                    campaign_id,
                    item.symbol,
                    kind="analysis_error",
                    detail={"error": str(exc)},
                    at=instant,
                )
                continue

            run_id = self._record_run(result)
            verdict, reasons = self._actionability(
                result, item.negative_strength, config, open_positions
            )
            trigger = None
            if verdict == "CANDIDATE" and result.plan is not None:
                trigger = self._trigger_plan(result, context, trade_date, config)
                if trigger is None:
                    verdict = "REJECTED"
                    reasons = ["no_previous_session"]
            status = "WATCHING" if verdict == "CANDIDATE" else "BLOCKED"
            if verdict == "CANDIDATE":
                candidates += 1
                open_positions += 1
            else:
                blocked += 1
            self.store.upsert_item(
                campaign_id=campaign_id,
                symbol=item.symbol,
                rank=rank,
                catalyst_score=item.score,
                events=events,
                verdict=verdict,
                status=status,
                analysis_run_id=run_id,
                plan=None if result.plan is None else result.plan.to_dict(),
                trigger=trigger,
            )
            self.store.append_event(
                campaign_id,
                item.symbol,
                kind="planned" if verdict == "CANDIDATE" else "rejected",
                detail={
                    "reasons": reasons,
                    "signal": "NONE"
                    if result.plan is None
                    else result.plan.signal.value,
                    "composite": result.report.composite,
                },
                at=instant,
            )

        cancel_stale({item.symbol for item in shortlist.items})
        summary.update(
            {
                "candidates": candidates,
                "blocked": blocked,
                "errors": errors,
                "equity_paise": equity,
                "available_cash_paise": cash,
                "open_positions_before": self._open_position_count(config.account_id),
            }
        )
        self.store.set_summary(campaign_id, summary)
        self.store.set_status(campaign_id, "ACTIVE" if candidates else "READY")
        return self.store.get(campaign_id)

    def latest(self) -> Campaign | None:
        return self.store.latest()

    def get(self, campaign_id: str) -> Campaign:
        return self.store.get(campaign_id)

    def cancel(self, campaign_id: str, *, as_of: datetime | None = None) -> Campaign:
        campaign = self.store.get(campaign_id)
        if campaign.status in {"CLOSED", "CANCELLED"}:
            return campaign
        instant = core_utc(as_of or self.clock.now())
        for item in campaign.items:
            if item.status == "WATCHING":
                self.store.update_item(campaign_id, item.symbol, status="CANCELLED")
                self.store.append_event(
                    campaign_id,
                    item.symbol,
                    kind="cancelled",
                    detail={"by": "owner"},
                    at=instant,
                )
        self.store.set_status(campaign_id, "CANCELLED")
        return self.store.get(campaign_id)

    def review(self, campaign_id: str) -> dict[str, object]:
        campaign = self.store.get(campaign_id)
        review = campaign_review(
            self.path,
            trade_date=campaign.trade_date,
            account_id=self.config.account_id,
        )
        return {**review, "events": list(self.store.events(campaign_id))}

    def _ensure_news(
        self, store: NewsStore, as_of: datetime, config: CampaignConfig
    ) -> dict[str, object]:
        app = self.settings.load()
        fetched = stored = duplicates = 0
        errors: list[str] = []
        for url in config.feed_urls:
            try:
                payload = self._news_fetcher(url)
                entries = parse_feed(payload, ingested_at=as_of)
            except NewsError as exc:
                errors.append(f"{url}: {exc}")
                continue
            if not entries:
                errors.append(f"{url}: feed returned no items")
                continue
            source = urlsplit(url).hostname or "feed"
            result = store.ingest(
                entries[:MAX_ITEMS_PER_INGEST],
                source_id=source,
                agent_id="news-research",
                memory_ttl=timedelta(days=app.news_memory_ttl_days),
            )
            fetched += result.fetched
            stored += result.stored
            duplicates += result.duplicates
        return {
            "ingested": stored > 0,
            "feeds": len(config.feed_urls),
            "fetched": fetched,
            "stored": stored,
            "duplicates": duplicates,
            "errors": errors,
        }

    def _bars_context(
        self, symbols: Sequence[str], trade_date: date, as_of: datetime
    ) -> dict[str, _BarsContext]:
        if not symbols:
            return {}
        router = self.router()
        start = as_of - timedelta(days=120)
        contexts: dict[str, _BarsContext] = {}
        for symbol in symbols[:MAX_SHORTLIST]:
            try:
                bars = router.bars(
                    resolve(symbol),
                    interval=BarInterval.DAY_1,
                    start=start,
                    end=as_of,
                ).data
            except Exception:  # a vendor failure drops one name, not the campaign
                continue
            completed = tuple(
                bar
                for bar in bars
                if core_exchange_time(bar.event_at).date() < trade_date
            )
            if not completed:
                continue
            window = completed[-20:]
            previous = window[-1]
            contexts[symbol] = _BarsContext(
                last_close_paise=paise_from_price(previous.close).value,
                avg_volume_20d=sum(bar.volume for bar in window) // len(window),
                previous_high_paise=paise_from_price(previous.high).value,
                previous_close_paise=paise_from_price(previous.close).value,
            )
        return contexts

    def _equity_and_cash(self, config: CampaignConfig) -> tuple[int, int]:
        try:
            balance = PaperAccountStore(self.path, self.clock).get_balance(
                config.account_id
            )
        except PaperAccountError:
            return config.equity_paise, config.available_cash_paise
        if balance.balance_paise <= 0:
            return config.equity_paise, config.available_cash_paise
        equity = min(config.equity_paise, balance.balance_paise)
        cash = min(config.available_cash_paise, balance.balance_paise)
        return equity, cash

    def _open_position_count(self, account_id: str) -> int:
        with closing(core_open_connection(self.path, foreign_keys=True)) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM paper_positions "
                "WHERE account_id = ? AND quantity > 0",
                (account_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def _record_run(self, result: AnalysisResult) -> str | None:
        app = self.settings.load()
        try:
            stored = AnalysisRunStore(self.path).record(
                symbol=result.symbol,
                timeframe=result.timeframe,
                as_of=result.as_of,
                started_at=result.as_of,
                vendor_quotes=result.vendor_quotes,
                vendor_daily=result.vendor_daily,
                is_realtime=result.is_realtime,
                data_freshness=(
                    "realtime_kite" if result.is_realtime else "delayed_yfinance"
                ),
                bar_count=result.report.bar_count,
                facts=result.report.facts(),
                composite=result.report.composite,
                debate_attempted=result.bull is not None or result.bear is not None,
                debate_rounds=result.debate_rounds,
                mode=result.mode,
                provider=app.provider.value,
                model=app.model_name,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                signal=("HOLD" if result.plan is None else result.plan.signal.value),
                plan=None if result.plan is None else result.plan.to_dict(),
                vetoes=result.vetoes,
                detail=result.to_dict(),
            )
        except (AnalysisRunError, ValueError):
            return None
        return stored.run_id

    def _actionability(
        self,
        result: AnalysisResult,
        negative_strength: int,
        config: CampaignConfig,
        open_positions: int,
    ) -> tuple[str, list[str]]:
        reasons: list[str] = []
        plan = result.plan
        if plan is None or plan.signal.value != "BUY":
            reasons.append("signal_not_buy")
        if result.report.composite is None or result.report.composite <= 0:
            reasons.append("composite_not_positive")
        if result.report.trend is None or result.report.trend.value <= 0:
            reasons.append("trend_not_positive")
        for veto in result.vetoes:
            if veto in BLOCKING_VETOES:
                reasons.append(veto)
        if plan is not None and plan.reward_risk_bps < MIN_REWARD_RISK_BPS:
            reasons.append("reward_risk_below_floor")
        if negative_strength > 0:
            reasons.append("negative_catalyst")
        confidence = result.confidence
        if confidence is not None:
            if confidence.assessment.disposition is ConfidenceDisposition.DISCARD:
                reasons.append("confidence_discarded")
        if open_positions >= config.max_open_positions:
            reasons.append("max_open_positions")
        return ("REJECTED" if reasons else "CANDIDATE"), reasons

    def _trigger_plan(
        self,
        result: AnalysisResult,
        context: _BarsContext | None,
        trade_date: date,
        config: CampaignConfig,
    ) -> dict[str, object] | None:
        plan = result.plan
        if plan is None or context is None:
            return None
        stop_paise = paise_from_price(plan.stop_loss).value
        if config.trigger_mode == "pullback":
            level = paise_from_price(plan.entry_price).value
        else:
            level = context.previous_high_paise
        risk = level - stop_paise
        if risk <= 0:
            return None
        target_paise = level + risk * MIN_REWARD_RISK_BPS // 10_000
        return {
            "mode": config.trigger_mode,
            "level_paise": level,
            "stop_paise": stop_paise,
            "target_paise": target_paise,
            "rr_bps": MIN_REWARD_RISK_BPS,
            "entry_reference_paise": paise_from_price(plan.entry_price).value,
            "previous_high_paise": context.previous_high_paise,
            "previous_close_paise": context.previous_close_paise,
            "max_chase_bps": config.max_chase_bps,
            "valid_until": _valid_until(
                trade_date, config.valid_until_time
            ).isoformat(),
            "quantity": plan.suggested_quantity,
        }


__all__ = [
    "BLOCKING_VETOES",
    "CAMPAIGN_PREFIX",
    "Campaign",
    "CampaignError",
    "CampaignItem",
    "CampaignNotFound",
    "CampaignService",
    "CampaignStore",
    "campaign_trade_date",
    "next_weekday",
]
