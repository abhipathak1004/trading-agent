"""Frozen opportunities, lifecycle events and counterfactual shadow trades.

The opportunity row is immutable. Lifecycle is an append-only event stream; a
shadow trade records what would have happened even when nothing was executed.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from bq.confidence import (
    ConfidenceAssessment,
    ConfidenceDisposition,
    ConfidenceLevel,
)
from bq.core import (
    IDENTIFIER,
    Clock,
    SystemClock,
    canonical_digest,
    open_connection,
    parse_timestamp,
    utc,
)
from bq.data.symbols import SymbolError
from bq.data.symbols import resolve as resolve_symbol
from bq.data.vendors import BarInterval, DataDomain, VendorRouter
from bq.infra.audit import append_event

MAX_DETAIL_JSON = 65_536
MAX_PLAN_JSON = 8_192
MAX_SHADOW_TRADES = 100
SHADOW_HORIZONS = (1, 3, 5, 10)
FILTER_HORIZON = 5


def _utc(value: datetime) -> datetime:
    try:
        return utc(value)
    except ValueError as exc:
        raise OpportunityError(
            "Opportunity timestamps must be timezone-aware."
        ) from exc


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return parse_timestamp(value)


def _json(value: object, *, limit: int) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(payload) > limit:
        raise OpportunityError("Opportunity detail exceeded the size limit.")
    return payload


class OpportunityHorizon(StrEnum):
    INTRADAY = "INTRADAY"
    SHORT_TERM = "SHORT_TERM"
    SWING = "SWING"
    MEDIUM = "MEDIUM"
    LONG = "LONG"


class OpportunitySide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OpportunityLifecycle(StrEnum):
    NEW = "NEW"
    WATCHING = "WATCHING"
    TRIGGERED = "TRIGGERED"
    TRADED = "TRADED"
    CLOSED = "CLOSED"
    DISMISSED = "DISMISSED"
    EXPIRED = "EXPIRED"


class OpportunityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OpportunityEvent:
    event_id: str
    opportunity_id: str
    lifecycle: OpportunityLifecycle
    occurred_at: datetime
    note: str | None


@dataclass(frozen=True, slots=True)
class Opportunity:
    opportunity_id: str
    instrument_id: str
    horizon: OpportunityHorizon
    side: OpportunitySide
    created_at: datetime
    assessment: ConfidenceAssessment
    assessment_hash: str
    run_id: str | None = None
    source: str | None = None
    setup_id: str | None = None
    confidence: dict[str, object] | None = None
    plan: dict[str, object] | None = None
    lifecycle: OpportunityLifecycle = OpportunityLifecycle.NEW

    @property
    def score(self) -> float:
        """Ranking only; confidence still decides whether this is suggested."""
        probability = self.assessment.probability
        ev = max(0.0, self.assessment.ev_r_after_costs)
        families = min(1.0, self.assessment.confirming_families / 7.0)
        raw = 100.0 * (0.60 * probability + 0.20 * min(1.0, ev / 2.0) + 0.20 * families)
        return round(raw - 10.0 * len(self.assessment.vetoes), 2)

    def to_dict(self) -> dict[str, object]:
        return {
            "opportunity_id": self.opportunity_id,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "side": self.side.value,
            "created_at": self.created_at.isoformat(),
            "probability": self.assessment.probability,
            "level": self.assessment.level.value,
            "disposition": self.assessment.disposition.value,
            "confirming_families": self.assessment.confirming_families,
            "base_rate_samples": self.assessment.base_rate_samples,
            "calibrated": self.assessment.calibrated,
            "ev_r_after_costs": self.assessment.ev_r_after_costs,
            "vetoes": list(self.assessment.vetoes),
            "assessment_hash": self.assessment_hash,
            "score": self.score,
            "run_id": self.run_id,
            "source": self.source,
            "setup_id": self.setup_id,
            "confidence": self.confidence,
            "plan": self.plan,
            "lifecycle": self.lifecycle.value,
        }


@dataclass(frozen=True, slots=True)
class ShadowTrade:
    shadow_id: str
    opportunity_id: str
    instrument_id: str
    horizon: OpportunityHorizon
    side: OpportunitySide
    as_of: datetime
    entry_price_paise: int
    stop_loss_paise: int
    target_price_paise: int
    quantity: int
    level: ConfidenceLevel
    probability: float
    suggested: bool
    not_taken_reason: str | None
    plan: dict[str, object]
    created_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "shadow_id": self.shadow_id,
            "opportunity_id": self.opportunity_id,
            "instrument_id": self.instrument_id,
            "horizon": self.horizon.value,
            "side": self.side.value,
            "as_of": self.as_of.isoformat(),
            "entry_price_paise": self.entry_price_paise,
            "stop_loss_paise": self.stop_loss_paise,
            "target_price_paise": self.target_price_paise,
            "quantity": self.quantity,
            "level": self.level.value,
            "probability": self.probability,
            "suggested": self.suggested,
            "not_taken_reason": self.not_taken_reason,
            "plan": self.plan,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    shadow_id: str
    horizon_days: int
    resolved_at: datetime
    outcome: int
    realized_r: float
    close_price_paise: int
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "shadow_id": self.shadow_id,
            "horizon_days": self.horizon_days,
            "resolved_at": self.resolved_at.isoformat(),
            "outcome": self.outcome,
            "realized_r": self.realized_r,
            "close_price_paise": self.close_price_paise,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ShadowSummary:
    verdict: str
    suggested_count: int
    unsuggested_count: int
    suggested_win_rate: float | None
    unsuggested_win_rate: float | None
    suggested_interval: tuple[float, float] | None
    unsuggested_interval: tuple[float, float] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "suggested_count": self.suggested_count,
            "unsuggested_count": self.unsuggested_count,
            "suggested_win_rate": self.suggested_win_rate,
            "unsuggested_win_rate": self.unsuggested_win_rate,
            "suggested_interval": list(self.suggested_interval)
            if self.suggested_interval is not None
            else None,
            "unsuggested_interval": list(self.unsuggested_interval)
            if self.unsuggested_interval is not None
            else None,
        }


def _wilson(wins: int, count: int) -> tuple[float, float] | None:
    if count <= 0:
        return None
    z = 1.96
    p = wins / count
    denominator = 1 + z * z / count
    centre = (p + z * z / (2 * count)) / denominator
    margin = (
        z * ((p * (1 - p) / count + z * z / (4 * count * count)) ** 0.5) / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


class OpportunityStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def freeze(
        self,
        *,
        opportunity_id: str,
        instrument_id: str,
        horizon: OpportunityHorizon,
        side: OpportunitySide,
        assessment: ConfidenceAssessment,
        run_id: str | None = None,
        source: str | None = None,
        setup_id: str | None = None,
        confidence: dict[str, object] | None = None,
        plan: dict[str, object] | None = None,
    ) -> Opportunity:
        if (
            not isinstance(opportunity_id, str)
            or IDENTIFIER.fullmatch(opportunity_id) is None
        ):
            raise OpportunityError("Invalid opportunity identifier.")
        if (
            not isinstance(instrument_id, str)
            or IDENTIFIER.fullmatch(instrument_id) is None
        ):
            raise OpportunityError("Invalid instrument identifier.")
        if not isinstance(horizon, OpportunityHorizon):
            raise OpportunityError("Invalid opportunity horizon.")
        if not isinstance(side, OpportunitySide):
            raise OpportunityError("Invalid opportunity side.")
        if not isinstance(assessment, ConfidenceAssessment):
            raise OpportunityError("A confidence assessment is required.")
        confidence_json = (
            None if confidence is None else _json(confidence, limit=MAX_DETAIL_JSON)
        )
        plan_json = None if plan is None else _json(plan, limit=MAX_PLAN_JSON)

        created_at = _utc(self.clock.now())
        payload = {
            "opportunity_id": opportunity_id,
            "instrument_id": instrument_id,
            "horizon": horizon.value,
            "side": side.value,
            "created_at": _timestamp(created_at),
            "probability": assessment.probability,
            "disposition": assessment.disposition.value,
            "level": assessment.level.value,
            "calibrated": assessment.calibrated,
            "ev_r_after_costs": assessment.ev_r_after_costs,
            "vetoes": list(assessment.vetoes),
        }
        assessment_hash = sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT assessment_hash FROM opportunities "
                    "WHERE opportunity_id = ?",
                    (opportunity_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != assessment_hash:
                        raise OpportunityError(
                            "Opportunity identity has conflicting content."
                        )
                    return self.get(opportunity_id)
                connection.execute(
                    "INSERT INTO opportunities "
                    "(opportunity_id, instrument_id, horizon, side, created_at, "
                    "probability, disposition, level, calibrated, ev_r, assessment_hash, "
                    "run_id, source, setup_id, confidence_json, plan_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        opportunity_id,
                        instrument_id,
                        horizon.value,
                        side.value,
                        _timestamp(created_at),
                        repr(assessment.probability),
                        assessment.disposition.value,
                        assessment.level.value,
                        1 if assessment.calibrated else 0,
                        repr(assessment.ev_r_after_costs),
                        assessment_hash,
                        run_id,
                        source,
                        setup_id,
                        confidence_json,
                        plan_json,
                    ),
                )
                self._append_event(
                    connection,
                    opportunity_id,
                    OpportunityLifecycle.NEW,
                    created_at,
                    None,
                )
                append_event(
                    connection,
                    occurred_at=created_at,
                    actor="system",
                    action="opportunity.frozen",
                    object_type="opportunity",
                    object_id=opportunity_id,
                    payload={"assessment_hash": assessment_hash},
                )
        return self.get(opportunity_id)

    def get(self, opportunity_id: str) -> Opportunity:
        if (
            not isinstance(opportunity_id, str)
            or IDENTIFIER.fullmatch(opportunity_id) is None
        ):
            raise OpportunityError("Invalid opportunity identifier.")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT opportunity_id, instrument_id, horizon, side, created_at, "
                "probability, disposition, level, calibrated, ev_r, assessment_hash, "
                "run_id, source, setup_id, confidence_json, plan_json "
                "FROM opportunities WHERE opportunity_id = ?",
                (opportunity_id,),
            ).fetchone()
            if row is None:
                raise OpportunityError("Opportunity not found.")
            lifecycle = self._lifecycle(connection, opportunity_id)
        return self._decode(row, lifecycle)

    def list(
        self,
        *,
        limit: int = 50,
        horizon: OpportunityHorizon | None = None,
        level: ConfidenceLevel | None = None,
        lifecycle: OpportunityLifecycle | None = None,
    ) -> tuple[Opportunity, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise OpportunityError("Opportunity limit must be between 1 and 100.")
        if horizon is not None and not isinstance(horizon, OpportunityHorizon):
            raise OpportunityError("Invalid opportunity horizon.")
        if level is not None and not isinstance(level, ConfidenceLevel):
            raise OpportunityError("Invalid confidence level.")
        if lifecycle is not None and not isinstance(lifecycle, OpportunityLifecycle):
            raise OpportunityError("Invalid opportunity lifecycle.")
        clauses: list[str] = []
        params: list[object] = []
        if horizon is not None:
            clauses.append("horizon = ?")
            params.append(horizon.value)
        if level is not None:
            clauses.append("level = ?")
            params.append(level.value)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT opportunity_id, instrument_id, horizon, side, created_at, "
                "probability, disposition, level, calibrated, ev_r, assessment_hash, "
                "run_id, source, setup_id, confidence_json, plan_json "
                f"FROM opportunities {where}"
                "ORDER BY created_at DESC, opportunity_id DESC LIMIT ?",
                (*params, limit * 2),
            ).fetchall()
            records: list[Opportunity] = []
            for row in rows:
                current = self._lifecycle(connection, str(row[0]))
                if lifecycle is None or current is lifecycle:
                    records.append(self._decode(row, current))
                if len(records) >= limit:
                    break
        return tuple(
            sorted(records, key=lambda item: (-item.score, item.opportunity_id))
        )

    def events(self, opportunity_id: str) -> tuple[OpportunityEvent, ...]:
        if IDENTIFIER.fullmatch(opportunity_id) is None:
            raise OpportunityError("Invalid opportunity identifier.")
        with closing(self._connect()) as connection:
            self._require(connection, opportunity_id)
            rows = connection.execute(
                "SELECT event_id, opportunity_id, lifecycle, occurred_at, note "
                "FROM opportunity_events WHERE opportunity_id = ? "
                "ORDER BY rowid",
                (opportunity_id,),
            ).fetchall()
        return tuple(
            OpportunityEvent(
                event_id=str(row[0]),
                opportunity_id=str(row[1]),
                lifecycle=OpportunityLifecycle(str(row[2])),
                occurred_at=_parse_timestamp(str(row[3])),
                note=str(row[4]) if row[4] is not None else None,
            )
            for row in rows
        )

    def transition(
        self,
        opportunity_id: str,
        target: OpportunityLifecycle,
        *,
        note: str | None = None,
    ) -> Opportunity:
        if IDENTIFIER.fullmatch(opportunity_id) is None:
            raise OpportunityError("Invalid opportunity identifier.")
        if not isinstance(target, OpportunityLifecycle):
            raise OpportunityError("Invalid opportunity lifecycle.")
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            raise OpportunityError("Opportunity note must be at most 500 characters.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                self._require(connection, opportunity_id)
                current = self._lifecycle(connection, opportunity_id)
                allowed = {
                    OpportunityLifecycle.NEW: {
                        OpportunityLifecycle.WATCHING,
                        OpportunityLifecycle.TRIGGERED,
                        OpportunityLifecycle.DISMISSED,
                        OpportunityLifecycle.EXPIRED,
                    },
                    OpportunityLifecycle.WATCHING: {
                        OpportunityLifecycle.TRIGGERED,
                        OpportunityLifecycle.DISMISSED,
                        OpportunityLifecycle.EXPIRED,
                    },
                    OpportunityLifecycle.TRIGGERED: {
                        OpportunityLifecycle.TRADED,
                        OpportunityLifecycle.DISMISSED,
                        OpportunityLifecycle.EXPIRED,
                    },
                    OpportunityLifecycle.TRADED: {OpportunityLifecycle.CLOSED},
                }
                if target is current:
                    return self.get(opportunity_id)
                if target not in allowed.get(current, set()):
                    raise OpportunityError(
                        f"Cannot move opportunity from {current.value} to {target.value}."
                    )
                now = _utc(self.clock.now())
                self._append_event(connection, opportunity_id, target, now, note)
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="opportunity.transitioned",
                    object_type="opportunity",
                    object_id=opportunity_id,
                    payload={"lifecycle": target.value},
                )
        return self.get(opportunity_id)

    def record_shadow(
        self,
        opportunity: Opportunity,
        *,
        as_of: datetime,
        entry_price_paise: int,
        stop_loss_paise: int,
        target_price_paise: int,
        quantity: int,
        not_taken_reason: str | None,
        plan: dict[str, object],
    ) -> ShadowTrade:
        if not isinstance(opportunity, Opportunity):
            raise OpportunityError("An opportunity is required.")
        for label, value in (
            ("entry", entry_price_paise),
            ("stop", stop_loss_paise),
            ("target", target_price_paise),
            ("quantity", quantity),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise OpportunityError(f"Shadow {label} must be positive.")
        plan_json = _json(plan, limit=MAX_PLAN_JSON)
        suggested = opportunity.assessment.disposition is ConfidenceDisposition.SUGGEST
        now = _utc(self.clock.now())
        shadow_id = (
            "sh_"
            + canonical_digest(
                {
                    "opportunity_id": opportunity.opportunity_id,
                    "as_of": _timestamp(as_of),
                }
            )[:32]
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT shadow_id FROM shadow_trades WHERE opportunity_id = ?",
                    (opportunity.opportunity_id,),
                ).fetchone()
                if existing is not None:
                    return self._get_shadow(connection, str(existing[0]))
                connection.execute(
                    "INSERT INTO shadow_trades "
                    "(shadow_id, opportunity_id, instrument_id, horizon, side, as_of, "
                    "entry_price_paise, stop_loss_paise, target_price_paise, quantity, "
                    "level, probability, suggested, not_taken_reason, plan_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        shadow_id,
                        opportunity.opportunity_id,
                        opportunity.instrument_id,
                        opportunity.horizon.value,
                        opportunity.side.value,
                        _timestamp(as_of),
                        entry_price_paise,
                        stop_loss_paise,
                        target_price_paise,
                        quantity,
                        opportunity.assessment.level.value,
                        repr(opportunity.assessment.probability),
                        1 if suggested else 0,
                        not_taken_reason,
                        plan_json,
                        _timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="system",
                    action="shadow.recorded",
                    object_type="shadow_trade",
                    object_id=shadow_id,
                    payload={"opportunity_id": opportunity.opportunity_id},
                )
                return self._get_shadow(connection, shadow_id)

    def shadow_trades(
        self, *, limit: int = MAX_SHADOW_TRADES
    ) -> tuple[ShadowTrade, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SHADOW_TRADES
        ):
            raise OpportunityError("Shadow limit is out of range.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT shadow_id FROM shadow_trades "
                "ORDER BY as_of DESC, shadow_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(self._get_shadow(connection, str(row[0])) for row in rows)

    def shadow_outcomes(self, shadow_id: str) -> tuple[ShadowOutcome, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT shadow_id, horizon_days, resolved_at, outcome, realized_r, "
                "close_price_paise, source FROM shadow_outcomes "
                "WHERE shadow_id = ? ORDER BY horizon_days",
                (shadow_id,),
            ).fetchall()
        return tuple(
            ShadowOutcome(
                shadow_id=str(row[0]),
                horizon_days=int(row[1]),
                resolved_at=_parse_timestamp(str(row[2])),
                outcome=int(row[3]),
                realized_r=float(row[4]),
                close_price_paise=int(row[5]),
                source=str(row[6]),
            )
            for row in rows
        )

    def record_shadow_outcome(
        self,
        shadow_id: str,
        *,
        horizon_days: int,
        resolved_at: datetime,
        outcome: int,
        realized_r: float,
        close_price_paise: int,
        source: str = "shadow_resolver",
    ) -> ShadowOutcome:
        if horizon_days not in SHADOW_HORIZONS:
            raise OpportunityError("Shadow horizon is not supported.")
        if outcome not in (0, 1):
            raise OpportunityError("Shadow outcome must be 0 or 1.")
        if close_price_paise <= 0:
            raise OpportunityError("Shadow close price must be positive.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                self._require_shadow(connection, shadow_id)
                existing = connection.execute(
                    "SELECT shadow_id FROM shadow_outcomes "
                    "WHERE shadow_id = ? AND horizon_days = ?",
                    (shadow_id, horizon_days),
                ).fetchone()
                if existing is not None:
                    outcomes = self.shadow_outcomes(shadow_id)
                    return next(
                        item for item in outcomes if item.horizon_days == horizon_days
                    )
                connection.execute(
                    "INSERT INTO shadow_outcomes "
                    "(shadow_id, horizon_days, resolved_at, outcome, realized_r, "
                    "close_price_paise, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        shadow_id,
                        horizon_days,
                        _timestamp(resolved_at),
                        outcome,
                        repr(realized_r),
                        close_price_paise,
                        source,
                    ),
                )
        return next(
            item
            for item in self.shadow_outcomes(shadow_id)
            if item.horizon_days == horizon_days
        )

    def filter_verdict(self) -> ShadowSummary:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT s.suggested, o.outcome FROM shadow_trades s "
                "JOIN shadow_outcomes o ON o.shadow_id = s.shadow_id "
                "WHERE o.horizon_days = ?",
                (FILTER_HORIZON,),
            ).fetchall()
        suggested = [int(row[1]) for row in rows if int(row[0]) == 1]
        unsuggested = [int(row[1]) for row in rows if int(row[0]) == 0]
        suggested_interval = _wilson(sum(suggested), len(suggested))
        unsuggested_interval = _wilson(sum(unsuggested), len(unsuggested))
        verdict = "NEUTRAL"
        if suggested_interval and unsuggested_interval:
            if suggested_interval[0] > unsuggested_interval[1]:
                verdict = "HELPS"
            elif unsuggested_interval[0] > suggested_interval[1]:
                verdict = "HURTS"
        return ShadowSummary(
            verdict=verdict,
            suggested_count=len(suggested),
            unsuggested_count=len(unsuggested),
            suggested_win_rate=(sum(suggested) / len(suggested)) if suggested else None,
            unsuggested_win_rate=(sum(unsuggested) / len(unsuggested))
            if unsuggested
            else None,
            suggested_interval=suggested_interval,
            unsuggested_interval=unsuggested_interval,
        )

    def resolve_shadows(
        self,
        router: VendorRouter,
        *,
        as_of: datetime,
        max_trades: int = 50,
    ) -> dict[str, object]:
        if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
            raise OpportunityError("Shadow resolution needs a timezone-aware as_of.")
        if (
            isinstance(max_trades, bool)
            or not isinstance(max_trades, int)
            or not 1 <= max_trades <= MAX_SHADOW_TRADES
        ):
            raise OpportunityError("Shadow resolution limit is out of range.")
        rows = self.shadow_trades(limit=max_trades)
        examined = resolved = skipped = failed = 0
        notes: list[str] = []
        for shadow in rows:
            examined += 1
            existing = {
                item.horizon_days for item in self.shadow_outcomes(shadow.shadow_id)
            }
            for horizon in SHADOW_HORIZONS:
                due_at = shadow.as_of + timedelta(days=horizon)
                if horizon in existing or due_at > as_of:
                    continue
                try:
                    symbol = resolve_symbol(shadow.instrument_id)
                    result = router.bars(
                        symbol,
                        interval=BarInterval.DAY_1,
                        start=shadow.as_of,
                        end=min(as_of, due_at),
                        domain=DataDomain.BARS_DAILY,
                    )
                    bars = tuple(
                        bar
                        for bar in result.data
                        if shadow.as_of < bar.event_at <= due_at
                    )
                    if not bars:
                        skipped += 1
                        notes.append(f"{shadow.instrument_id}/{horizon}d: no bars")
                        continue
                    outcome, realized_r, close_paise, resolved_at = (
                        self._resolve_shadow(shadow, bars)
                    )
                    self.record_shadow_outcome(
                        shadow.shadow_id,
                        horizon_days=horizon,
                        resolved_at=resolved_at,
                        outcome=outcome,
                        realized_r=realized_r,
                        close_price_paise=close_paise,
                    )
                    resolved += 1
                except (SymbolError, ValueError) as exc:
                    skipped += 1
                    notes.append(f"{shadow.instrument_id}/{horizon}d: {exc}")
                except Exception as exc:  # noqa: BLE001 - one vendor failure is isolated
                    failed += 1
                    notes.append(
                        f"{shadow.instrument_id}/{horizon}d: {type(exc).__name__}"
                    )
        return {
            "examined": examined,
            "resolved": resolved,
            "skipped": skipped,
            "failed": failed,
            "notes": notes,
        }

    @staticmethod
    def _resolve_shadow(
        shadow: ShadowTrade,
        bars: tuple[object, ...],
    ) -> tuple[int, float, int, datetime]:
        from bq.core import paise_from_price

        risk = shadow.entry_price_paise - shadow.stop_loss_paise
        if risk <= 0:
            raise OpportunityError("Shadow has invalid plan geometry.")
        for bar in bars:
            low = paise_from_price(bar.low).value  # type: ignore[attr-defined]
            high = paise_from_price(bar.high).value  # type: ignore[attr-defined]
            if low <= shadow.stop_loss_paise:
                return 0, -1.0, low, bar.event_at  # type: ignore[attr-defined]
            if high >= shadow.target_price_paise:
                return (
                    1,
                    (shadow.target_price_paise - shadow.entry_price_paise) / risk,
                    high,
                    bar.event_at,  # type: ignore[attr-defined]
                )
        last = bars[-1]
        close = paise_from_price(last.close).value  # type: ignore[attr-defined]
        outcome = 1 if close > shadow.entry_price_paise else 0
        realized = (close - shadow.entry_price_paise) / risk
        return outcome, realized, close, last.event_at  # type: ignore[attr-defined]

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        opportunity_id: str,
        lifecycle: OpportunityLifecycle,
        occurred_at: datetime,
        note: str | None,
    ) -> None:
        connection.execute(
            "INSERT INTO opportunity_events "
            "(event_id, opportunity_id, lifecycle, occurred_at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                secrets.token_urlsafe(18),
                opportunity_id,
                lifecycle.value,
                _timestamp(occurred_at),
                note,
            ),
        )

    @staticmethod
    def _lifecycle(
        connection: sqlite3.Connection, opportunity_id: str
    ) -> OpportunityLifecycle:
        row = connection.execute(
            "SELECT lifecycle FROM opportunity_events WHERE opportunity_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (opportunity_id,),
        ).fetchone()
        return (
            OpportunityLifecycle(str(row[0]))
            if row is not None
            else OpportunityLifecycle.NEW
        )

    @staticmethod
    def _require(connection: sqlite3.Connection, opportunity_id: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM opportunities WHERE opportunity_id = ?",
                (opportunity_id,),
            ).fetchone()
            is None
        ):
            raise OpportunityError("Opportunity not found.")

    @staticmethod
    def _require_shadow(connection: sqlite3.Connection, shadow_id: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM shadow_trades WHERE shadow_id = ?", (shadow_id,)
            ).fetchone()
            is None
        ):
            raise OpportunityError("Shadow trade not found.")

    @staticmethod
    def _decode(
        row: tuple[object, ...], lifecycle: OpportunityLifecycle
    ) -> Opportunity:
        assessment = ConfidenceAssessment(
            probability=float(str(row[5])),
            level=ConfidenceLevel(str(row[7])),
            disposition=ConfidenceDisposition(str(row[6])),
            confirming_families=0,
            base_rate_samples=0,
            live_outcomes=0,
            calibrated=bool(row[8]),
            ev_r_after_costs=float(str(row[9])),
            vetoes=(),
        )
        return Opportunity(
            opportunity_id=str(row[0]),
            instrument_id=str(row[1]),
            horizon=OpportunityHorizon(str(row[2])),
            side=OpportunitySide(str(row[3])),
            created_at=_parse_timestamp(str(row[4])),
            assessment=assessment,
            assessment_hash=str(row[10]),
            run_id=str(row[11]) if row[11] is not None else None,
            source=str(row[12]) if row[12] is not None else None,
            setup_id=str(row[13]) if row[13] is not None else None,
            confidence=json.loads(str(row[14])) if row[14] is not None else None,
            plan=json.loads(str(row[15])) if row[15] is not None else None,
            lifecycle=lifecycle,
        )

    def _get_shadow(
        self, connection: sqlite3.Connection, shadow_id: str
    ) -> ShadowTrade:
        row = connection.execute(
            "SELECT shadow_id, opportunity_id, instrument_id, horizon, side, as_of, "
            "entry_price_paise, stop_loss_paise, target_price_paise, quantity, level, "
            "probability, suggested, not_taken_reason, plan_json, created_at "
            "FROM shadow_trades WHERE shadow_id = ?",
            (shadow_id,),
        ).fetchone()
        if row is None:
            raise OpportunityError("Shadow trade not found.")
        return ShadowTrade(
            shadow_id=str(row[0]),
            opportunity_id=str(row[1]),
            instrument_id=str(row[2]),
            horizon=OpportunityHorizon(str(row[3])),
            side=OpportunitySide(str(row[4])),
            as_of=_parse_timestamp(str(row[5])),
            entry_price_paise=int(row[6]),
            stop_loss_paise=int(row[7]),
            target_price_paise=int(row[8]),
            quantity=int(row[9]),
            level=ConfidenceLevel(str(row[10])),
            probability=float(str(row[11])),
            suggested=bool(row[12]),
            not_taken_reason=str(row[13]) if row[13] is not None else None,
            plan=json.loads(str(row[14])),
            created_at=_parse_timestamp(str(row[15])),
        )


__all__ = [
    "FILTER_HORIZON",
    "Opportunity",
    "OpportunityError",
    "OpportunityEvent",
    "OpportunityHorizon",
    "OpportunityLifecycle",
    "OpportunitySide",
    "OpportunityStore",
    "SHADOW_HORIZONS",
    "ShadowOutcome",
    "ShadowSummary",
    "ShadowTrade",
]
