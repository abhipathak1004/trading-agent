"""Campaign outcome reconciliation and the learned catalyst weights.

The evening report must be able to answer "what should change tomorrow?" from
deterministic evidence, not vibes. This module computes:

* **Event hit rates** from campaign positions that have actually closed, with a
  Wilson 95% interval.
* **Bayesian-smoothed event multipliers** the shortlist applies the next day.
* **Reconciliation** for campaign positions that were closed by something other
  than the trigger engine (a server stop, a manual exit), using the real paper
  fills rather than a simulated first touch.

No model calls. Every figure is integer paise, a count, or a ratio derived from
them.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from bq.catalysts import POSITIVE_EVENTS, CatalystEvent
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    parse_timestamp as core_parse_timestamp,
)
from bq.core import (
    utc as core_utc,
)
from bq.data.symbols import SymbolError, resolve
from bq.data.vendors import VendorRouter
from bq.data.vendors.base import BarInterval
from bq.data.vendors.errors import VendorError

MAX_REVIEW_ITEMS = 200
MAX_STATS_ITEMS = 1000
PRIOR_HIT_RATE = 0.5
PRIOR_STRENGTH = 20.0
MIN_WEIGHT = 0.5
MAX_WEIGHT = 1.5


@dataclass(frozen=True, slots=True)
class EventStat:
    event: str
    closed: int
    wins: int
    hit_rate: float
    hit_rate_low: float
    hit_rate_high: float
    avg_r: float

    def to_dict(self) -> dict[str, object]:
        return {
            "event": self.event,
            "closed": self.closed,
            "wins": self.wins,
            "hit_rate": round(self.hit_rate, 4),
            "hit_rate_wilson_95": [
                round(self.hit_rate_low, 4),
                round(self.hit_rate_high, 4),
            ],
            "avg_r": round(self.avg_r, 4),
        }


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    examined: int
    reconciled: int
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "examined": self.examined,
            "reconciled": self.reconciled,
            "notes": list(self.notes),
        }


def _connect(path: Path) -> sqlite3.Connection:
    return core_open_connection(path, foreign_keys=True)


def _wilson(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    p = wins / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (
        z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def event_statistics(
    path: Path, *, limit: int = MAX_STATS_ITEMS
) -> tuple[EventStat, ...]:
    """Hit rate and average R per positive catalyst, over closed positions."""
    with closing(_connect(path)) as connection:
        rows = connection.execute(
            "SELECT events_json, r_multiple FROM campaign_items "
            "WHERE r_multiple IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(limit, 10_000)),),
        ).fetchall()
    samples: dict[str, list[float]] = {}
    for events_json, r_multiple in rows:
        value = float(r_multiple)
        try:
            events = json.loads(str(events_json))
        except json.JSONDecodeError:
            continue
        if not isinstance(events, list):
            continue
        seen: set[str] = set()
        for event in events:
            if not isinstance(event, Mapping):
                continue
            name = str(event.get("event", ""))
            if name in seen:
                continue
            try:
                kind = CatalystEvent(name)
            except ValueError:
                continue
            if kind not in POSITIVE_EVENTS:
                continue
            seen.add(name)
            samples.setdefault(name, []).append(value)
    stats: list[EventStat] = []
    for name, values in samples.items():
        wins = sum(1 for value in values if value > 0)
        low, high = _wilson(wins, len(values))
        stats.append(
            EventStat(
                event=name,
                closed=len(values),
                wins=wins,
                hit_rate=wins / len(values),
                hit_rate_low=low,
                hit_rate_high=high,
                avg_r=sum(values) / len(values),
            )
        )
    stats.sort(key=lambda item: (-item.closed, item.event))
    return tuple(stats)


def event_weights(
    path: Path, *, limit: int = MAX_STATS_ITEMS
) -> dict[CatalystEvent, float]:
    """Smoothed score multipliers for the shortlist.

    With no history every positive event keeps the neutral weight of 1.0. A
    small sample barely moves; a consistent winner or loser moves within
    ``MIN_WEIGHT..MAX_WEIGHT``.
    """
    weights: dict[CatalystEvent, float] = {}
    for stat in event_statistics(path, limit=limit):
        smoothed = (stat.wins + PRIOR_HIT_RATE * PRIOR_STRENGTH) / (
            stat.closed + PRIOR_STRENGTH
        )
        raw = smoothed / PRIOR_HIT_RATE if PRIOR_HIT_RATE else 1.0
        weights[CatalystEvent(stat.event)] = max(MIN_WEIGHT, min(MAX_WEIGHT, raw))
    return weights


def _campaign_row(
    connection: sqlite3.Connection, trade_date: date | None
) -> Sequence[object] | None:
    if trade_date is not None:
        return connection.execute(
            "SELECT campaign_id, trade_date, created_at, as_of, status, "
            "summary_json, config_json FROM campaigns WHERE trade_date = ?",
            (trade_date.isoformat(),),
        ).fetchone()
    return connection.execute(
        "SELECT campaign_id, trade_date, created_at, as_of, status, "
        "summary_json, config_json FROM campaigns "
        "ORDER BY trade_date DESC, created_at DESC LIMIT 1"
    ).fetchone()


def campaign_review(
    path: Path,
    *,
    trade_date: date | None = None,
    account_id: str = "paper-main",
) -> dict[str, object]:
    """A deterministic review of one campaign, safe for the evening report."""
    with closing(_connect(path)) as connection:
        row = _campaign_row(connection, trade_date)
        if row is None:
            return {"campaign": None, "items": [], "event_stats": [], "weights": {}}
        campaign_id = str(row[0])
        config = json.loads(str(row[6])) if row[6] is not None else {}
        resolved_account = str(config.get("account_id") or account_id)
        item_rows = connection.execute(
            "SELECT symbol, rank, events_json, verdict, status, plan_json, "
            "outcome_json, r_multiple FROM campaign_items "
            "WHERE campaign_id = ? ORDER BY rank, symbol LIMIT ?",
            (campaign_id, MAX_REVIEW_ITEMS),
        ).fetchall()
        fills = connection.execute(
            "SELECT o.instrument_id, o.side, f.quantity, f.price_paise, "
            "f.net_pnl_paise, f.filled_at FROM paper_fills f "
            "JOIN paper_orders o ON o.order_id = f.order_id "
            "WHERE o.client_order_id LIKE ? OR o.client_order_id LIKE ? "
            "ORDER BY f.filled_at LIMIT ?",
            (
                f"campaign-{campaign_id}-%",
                f"campaign-exit-{campaign_id}-%",
                MAX_REVIEW_ITEMS,
            ),
        ).fetchall()
        open_positions = connection.execute(
            "SELECT instrument_id, product, quantity FROM paper_product_positions "
            "WHERE account_id = ? AND quantity > 0 ORDER BY instrument_id",
            (resolved_account,),
        ).fetchall()

    items: list[dict[str, object]] = []
    realized = 0
    for item in item_rows:
        outcome = json.loads(str(item[6])) if item[6] is not None else None
        r_multiple = None if item[7] is None else float(item[7])
        if outcome:
            realized += int(outcome.get("net_pnl_paise") or 0)
        plan = json.loads(str(item[5])) if item[5] is not None else None
        items.append(
            {
                "symbol": str(item[0]),
                "rank": int(item[1]),
                "events": json.loads(str(item[2])),
                "verdict": str(item[3]),
                "status": str(item[4]),
                "signal": None if plan is None else plan.get("signal"),
                "entry_paise": None if not outcome else outcome.get("entry_paise"),
                "exit_paise": None if not outcome else outcome.get("exit_paise"),
                "exit_reason": None if not outcome else outcome.get("exit_reason"),
                "net_pnl_paise": None if not outcome else outcome.get("net_pnl_paise"),
                "r_multiple": r_multiple,
            }
        )
    stats = event_statistics(path)
    weights = event_weights(path)
    return {
        "campaign": {
            "campaign_id": campaign_id,
            "trade_date": str(row[1]),
            "as_of": str(row[3]),
            "status": str(row[4]),
            "summary": json.loads(str(row[5])),
        },
        "items": items,
        "realized_pnl_paise": realized,
        "fills": [
            {
                "symbol": str(fill[0]),
                "side": str(fill[1]),
                "quantity": int(fill[2]),
                "price_paise": int(fill[3]),
                "net_pnl_paise": int(fill[4]),
                "filled_at": str(fill[5]),
            }
            for fill in fills
        ],
        "open_positions": [
            {
                "symbol": str(position[0]),
                "product": str(position[1]),
                "quantity": int(position[2]),
            }
            for position in open_positions
        ],
        "event_stats": [stat.to_dict() for stat in stats],
        "weights": {event.value: round(value, 3) for event, value in weights.items()},
    }


def _held_quantity(
    connection: sqlite3.Connection, account_id: str, instrument_id: str
) -> int:
    row = connection.execute(
        "SELECT quantity FROM paper_positions "
        "WHERE account_id = ? AND instrument_id = ?",
        (account_id, instrument_id),
    ).fetchone()
    return int(row[0]) if row else 0


def _sell_fill(
    connection: sqlite3.Connection, instrument_id: str, *, after: datetime
) -> Sequence[object] | None:
    return connection.execute(
        "SELECT f.price_paise, f.net_pnl_paise, f.filled_at "
        "FROM paper_fills f JOIN paper_orders o ON o.order_id = f.order_id "
        "WHERE o.instrument_id = ? AND o.side = 'SELL' AND f.filled_at >= ? "
        "ORDER BY f.filled_at LIMIT 1",
        (
            instrument_id,
            after.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        ),
    ).fetchone()


def resolve_outcomes(
    path: Path,
    router: VendorRouter,
    *,
    as_of: datetime,
    horizon_days: int = 5,
    max_items: int = 50,
) -> ResolutionResult:
    """Reconcile campaign positions that are flat in the paper book.

    The trigger engine normally writes the outcome itself. This pass catches a
    position closed by a server stop, a manual exit, or an earlier build, so the
    hit-rate table does not silently miss those trades. It never sells anything.
    """
    from bq.campaign import CampaignStore  # local: avoids an import cycle

    instant = core_utc(as_of)
    notes: list[str] = []
    reconciled = 0
    store = CampaignStore(path)
    with closing(_connect(path)) as connection:
        rows = connection.execute(
            "SELECT i.campaign_id, i.symbol, i.plan_json, i.outcome_json, "
            "c.config_json FROM campaign_items i "
            "JOIN campaigns c ON c.campaign_id = i.campaign_id "
            "WHERE i.status = 'FILLED' ORDER BY i.updated_at LIMIT ?",
            (max(1, min(max_items, 500)),),
        ).fetchall()
        candidates = []
        for row in rows:
            outcome = json.loads(str(row[3])) if row[3] is not None else {}
            if outcome.get("state") == "CLOSED":
                continue
            plan = json.loads(str(row[2])) if row[2] is not None else None
            instrument = str((plan or {}).get("symbol") or row[1])
            config = json.loads(str(row[4])) if row[4] is not None else {}
            account = str(config.get("account_id") or "paper-main")
            if _held_quantity(connection, account, instrument) > 0:
                continue
            candidates.append((row[0], row[1], instrument, outcome))

    for campaign_id, symbol, instrument, outcome in candidates:
        entry_paise = int(outcome.get("entry_paise") or 0)
        stop_paise = int(outcome.get("stop_paise") or 0)
        target_paise = int(outcome.get("target_paise") or 0)
        filled_at_text = outcome.get("filled_at")
        if entry_paise <= 0 or stop_paise <= 0 or target_paise <= 0:
            continue
        try:
            filled_at = core_parse_timestamp(str(filled_at_text))
        except (TypeError, ValueError):
            filled_at = instant
        risk = max(1, entry_paise - stop_paise)
        exit_paise: int | None = None
        net_pnl: int | None = None
        reason = "reconciled"
        with closing(_connect(path)) as connection:
            sell = _sell_fill(connection, instrument, after=filled_at)
        if sell is not None:
            exit_paise = int(sell[0])
            net_pnl = int(sell[1])
        elif (instant - filled_at).days >= horizon_days:
            exit_paise, reason = _first_touch(
                router,
                instrument,
                entry_paise=entry_paise,
                stop_paise=stop_paise,
                target_paise=target_paise,
                filled_at=filled_at,
                horizon_days=horizon_days,
            )
        if exit_paise is None:
            notes.append(f"{symbol}: still open")
            continue
        r_multiple = (exit_paise - entry_paise) / risk
        merged = {
            **outcome,
            "state": "CLOSED",
            "exit_paise": exit_paise,
            "exit_reason": reason,
            "resolved_at": instant.isoformat(),
            "net_pnl_paise": net_pnl,
        }
        store.update_item(
            campaign_id,
            symbol,
            status="CLOSED",
            outcome=merged,
            r_multiple=r_multiple,
        )
        store.append_event(
            campaign_id,
            symbol,
            kind="reconciled",
            detail={
                "exit_paise": exit_paise,
                "reason": reason,
                "r_multiple": round(r_multiple, 4),
            },
            at=instant,
        )
        reconciled += 1
    return ResolutionResult(
        examined=len(candidates), reconciled=reconciled, notes=tuple(notes)
    )


def _first_touch(
    router: VendorRouter,
    instrument: str,
    *,
    entry_paise: int,
    stop_paise: int,
    target_paise: int,
    filled_at: datetime,
    horizon_days: int,
) -> tuple[int | None, str]:
    start = filled_at
    end = min(filled_at + timedelta(days=horizon_days), filled_at + timedelta(days=30))
    try:
        bars = router.bars(
            resolve(instrument), interval=BarInterval.DAY_1, start=start, end=end
        ).data
    except (SymbolError, VendorError, ValueError):
        return None, "no_data"
    completed = tuple(bar for bar in bars if bar.event_at > filled_at)
    if not completed:
        return None, "no_data"
    from bq.core import paise_from_price

    for bar in completed:
        if paise_from_price(bar.low).value <= stop_paise:
            return stop_paise, "stop"
        if paise_from_price(bar.high).value >= target_paise:
            return target_paise, "target"
    last = paise_from_price(completed[-1].close).value
    return last, "horizon"


__all__ = [
    "EventStat",
    "ResolutionResult",
    "campaign_review",
    "event_statistics",
    "event_weights",
    "resolve_outcomes",
]
