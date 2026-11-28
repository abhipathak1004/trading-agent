"""Episodic memory: decisions, shadow trades and calibrated outcomes.

An episode is immutable: it records what the system believed at ``as_of``. A
separate append-only outcome records what happened later. Brier calibration joins
the two and filters both sides against the requested point in time, so a later
resolution can never leak into an earlier evaluation.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from bq.analysis.decision import TradePlan
from bq.core import (
    Clock,
    SystemClock,
    canonical_digest,
    open_connection,
    paise_from_price,
    parse_timestamp,
    timestamp,
    utc,
)
from bq.decision.calibration import CalibrationSummary, summarize
from bq.infra.audit import append_event

MAX_DETAIL_CHARS = 16_384
MAX_MODEL_CHARS = 200
MAX_REGIME_CHARS = 32
MAX_SYMBOL_CHARS = 64
MAX_ID_CHARS = 128
MAX_OUTCOME_SOURCE_CHARS = 64


class MemoryError(ValueError):
    pass


class MissingEpisode(MemoryError):
    pass


class MemoryConflict(MemoryError):
    pass


class EpisodeSource(StrEnum):
    ANALYSIS = "analysis"
    STRATEGY = "strategy"
    MANUAL = "manual"


class EpisodeDecisionSource(StrEnum):
    JEV_NATIVE = "jev_native"
    LLM_FALLBACK = "llm_fallback"
    HEURISTIC = "heuristic"
    RULES_ONLY = "rules_only"
    LLM_DIRECT = "llm_direct"
    RULES = "rules"


class EpisodeAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    AVOID = "AVOID"
    SKIP = "SKIP"


class EpisodeStatus(StrEnum):
    SHADOW = "SHADOW"
    PROPOSED = "PROPOSED"
    EXECUTED = "EXECUTED"


def _text(value: object, *, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise MemoryError(f"{label} must be text.")
    text = value.strip()
    if not 1 <= len(text) <= limit:
        raise MemoryError(f"{label} must be between 1 and {limit} characters.")
    return text


def _aware(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise MemoryError(f"{label} must be timezone-aware.")
    return utc(value)


def _probability(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryError("Predicted probability must be a number or None.")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise MemoryError("Predicted probability must be in [0, 1].")
    return number


def _enum(value: object, enum_type: type[StrEnum], label: str) -> StrEnum:
    try:
        return enum_type(str(value))
    except ValueError as exc:
        raise MemoryError(f"Unknown {label}.") from exc


@dataclass(frozen=True, slots=True)
class Episode:
    episode_id: str
    run_id: str | None
    symbol: str
    as_of: datetime
    recorded_at: datetime
    source: EpisodeSource
    decision_source: EpisodeDecisionSource
    model: str
    regime: str | None
    action: EpisodeAction
    status: EpisodeStatus
    entry_price_paise: int | None
    stop_loss_paise: int | None
    target_price_paise: int | None
    quantity: int | None
    predicted_probability: float | None
    detail: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "run_id": self.run_id,
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "source": self.source.value,
            "decision_source": self.decision_source.value,
            "model": self.model,
            "regime": self.regime,
            "action": self.action.value,
            "status": self.status.value,
            "entry_price_paise": self.entry_price_paise,
            "stop_loss_paise": self.stop_loss_paise,
            "target_price_paise": self.target_price_paise,
            "quantity": self.quantity,
            "predicted_probability": self.predicted_probability,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class EpisodeOutcome:
    episode_id: str
    resolved_at: datetime
    recorded_at: datetime
    outcome: int
    realized_r: float
    close_price_paise: int | None
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "resolved_at": self.resolved_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "outcome": self.outcome,
            "realized_r": self.realized_r,
            "close_price_paise": self.close_price_paise,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    decision_source: str
    model: str
    summary: CalibrationSummary

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_source": self.decision_source,
            "model": self.model,
            "count": self.summary.count,
            "mean_brier": self.summary.mean_brier,
            "mean_log_loss": self.summary.mean_log_loss,
            "expected_calibration_error": (self.summary.expected_calibration_error),
            "outcome_rate": self.summary.outcome_rate,
        }


class EpisodicMemoryStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def record_shadow(
        self,
        *,
        run_id: str | None,
        symbol: str,
        as_of: datetime,
        source: EpisodeSource | str,
        decision_source: EpisodeDecisionSource | str,
        model: str,
        action: EpisodeAction | str,
        plan: TradePlan | None = None,
        predicted_probability: float | None = None,
        regime: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> Episode:
        return self.record(
            run_id=run_id,
            symbol=symbol,
            as_of=as_of,
            source=source,
            decision_source=decision_source,
            model=model,
            action=action,
            status=EpisodeStatus.SHADOW,
            plan=plan,
            predicted_probability=predicted_probability,
            regime=regime,
            detail=detail,
        )

    def record(
        self,
        *,
        run_id: str | None,
        symbol: str,
        as_of: datetime,
        source: EpisodeSource | str,
        decision_source: EpisodeDecisionSource | str,
        model: str,
        action: EpisodeAction | str,
        status: EpisodeStatus | str,
        plan: TradePlan | None = None,
        predicted_probability: float | None = None,
        regime: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> Episode:
        moment = _aware(as_of, label="as_of")
        recorded_at = utc(self.clock.now())
        if moment > recorded_at:
            raise MemoryError("A decision cannot be recorded before its as_of.")
        run = None
        if run_id is not None:
            run = _text(run_id, label="run_id", limit=MAX_ID_CHARS)
        symbol_text = _text(symbol, label="symbol", limit=MAX_SYMBOL_CHARS)
        model_text = _text(model, label="model", limit=MAX_MODEL_CHARS)
        regime_text = (
            None
            if regime is None
            else _text(regime, label="regime", limit=MAX_REGIME_CHARS)
        )
        source_value = _enum(source, EpisodeSource, "episode source")
        decision_value = _enum(
            decision_source, EpisodeDecisionSource, "decision source"
        )
        action_value = _enum(action, EpisodeAction, "episode action")
        status_value = _enum(status, EpisodeStatus, "episode status")
        probability = _probability(predicted_probability)
        detail_json = self._detail_json(detail)
        key = canonical_digest(
            {
                "run_id": run,
                "symbol": symbol_text,
                "as_of": moment.isoformat(),
                "source": source_value.value,
                "decision_source": decision_value.value,
                "model": model_text,
                "action": action_value.value,
                "status": status_value.value,
            }
        )
        episode_id = f"ep_{key[:32]}"
        entry = None if plan is None else paise_from_price(plan.entry_price).value
        stop = None if plan is None else paise_from_price(plan.stop_loss).value
        target = None if plan is None else paise_from_price(plan.target_price).value
        quantity = None if plan is None else plan.suggested_quantity
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT episode_id FROM episodic_memory WHERE episode_id = ?",
                    (episode_id,),
                ).fetchone()
                if existing is not None:
                    return self.get(episode_id)
                connection.execute(
                    "INSERT INTO episodic_memory ("
                    "episode_id, run_id, symbol, as_of, recorded_at, source, "
                    "decision_source, model, regime, action, status, "
                    "entry_price_paise, stop_loss_paise, target_price_paise, "
                    "quantity, predicted_probability, detail_json"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        episode_id,
                        run,
                        symbol_text,
                        timestamp(moment),
                        timestamp(recorded_at),
                        source_value.value,
                        decision_value.value,
                        model_text,
                        regime_text,
                        action_value.value,
                        status_value.value,
                        entry,
                        stop,
                        target,
                        quantity,
                        None if probability is None else repr(probability),
                        detail_json,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=recorded_at,
                    actor="system",
                    action="memory.episode.recorded",
                    object_type="episodic_memory",
                    object_id=episode_id,
                    payload={
                        "symbol": symbol_text,
                        "action": action_value.value,
                        "decision_source": decision_value.value,
                    },
                )
        return self.get(episode_id)

    def resolve(
        self,
        episode_id: str,
        *,
        resolved_at: datetime,
        outcome: int,
        realized_r: float,
        close_price_paise: int | None = None,
        source: str = "manual",
    ) -> EpisodeOutcome:
        identifier = _text(episode_id, label="episode_id", limit=MAX_ID_CHARS)
        moment = _aware(resolved_at, label="resolved_at")
        recorded_at = utc(self.clock.now())
        if moment > recorded_at:
            raise MemoryError("An outcome cannot be resolved in the future.")
        if isinstance(outcome, bool) or outcome not in (0, 1):
            raise MemoryError("An outcome must be 0 or 1.")
        if isinstance(realized_r, bool) or not isinstance(realized_r, (int, float)):
            raise MemoryError("realized_r must be a number.")
        realized = float(realized_r)
        if not math.isfinite(realized):
            raise MemoryError("realized_r must be finite.")
        source_text = _text(
            source, label="outcome source", limit=MAX_OUTCOME_SOURCE_CHARS
        )
        if close_price_paise is not None and close_price_paise <= 0:
            raise MemoryError("close_price_paise must be positive.")
        episode = self.get(identifier)
        if moment < episode.as_of:
            raise MemoryError("An outcome cannot precede its episode.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT episode_id FROM episode_outcomes WHERE episode_id = ?",
                    (identifier,),
                ).fetchone()
                if existing is not None:
                    return self.outcome(identifier)
                connection.execute(
                    "INSERT INTO episode_outcomes ("
                    "episode_id, resolved_at, recorded_at, outcome, realized_r, "
                    "close_price_paise, source"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        identifier,
                        timestamp(moment),
                        timestamp(recorded_at),
                        outcome,
                        repr(realized),
                        close_price_paise,
                        source_text,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=recorded_at,
                    actor="system",
                    action="memory.episode.resolved",
                    object_type="episode_outcome",
                    object_id=identifier,
                    payload={"outcome": str(outcome)},
                )
        return self.outcome(identifier)

    def get(self, episode_id: str) -> Episode:
        identifier = _text(episode_id, label="episode_id", limit=MAX_ID_CHARS)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT episode_id, run_id, symbol, as_of, recorded_at, source, "
                "decision_source, model, regime, action, status, "
                "entry_price_paise, stop_loss_paise, target_price_paise, "
                "quantity, predicted_probability, detail_json "
                "FROM episodic_memory WHERE episode_id = ?",
                (identifier,),
            ).fetchone()
        if row is None:
            raise MissingEpisode("No such episode.")
        return self._episode(row)

    def outcome(self, episode_id: str) -> EpisodeOutcome:
        identifier = _text(episode_id, label="episode_id", limit=MAX_ID_CHARS)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT episode_id, resolved_at, recorded_at, outcome, "
                "realized_r, close_price_paise, source FROM episode_outcomes "
                "WHERE episode_id = ?",
                (identifier,),
            ).fetchone()
        if row is None:
            raise MissingEpisode("No outcome for that episode.")
        return EpisodeOutcome(
            episode_id=str(row[0]),
            resolved_at=parse_timestamp(str(row[1])),
            recorded_at=parse_timestamp(str(row[2])),
            outcome=int(row[3]),
            realized_r=float(str(row[4])),
            close_price_paise=None if row[5] is None else int(row[5]),
            source=str(row[6]),
        )

    def episodes(
        self, *, limit: int = 50, as_of: datetime | None = None
    ) -> tuple[Episode, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise MemoryError("Episode limit must be between 1 and 500.")
        query = (
            "SELECT episode_id, run_id, symbol, as_of, recorded_at, source, "
            "decision_source, model, regime, action, status, "
            "entry_price_paise, stop_loss_paise, target_price_paise, "
            "quantity, predicted_probability, detail_json FROM episodic_memory "
        )
        params: list[object] = []
        if as_of is not None:
            query += "WHERE as_of <= ? "
            params.append(timestamp(_aware(as_of, label="as_of")))
        query += "ORDER BY as_of DESC, episode_id LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._episode(row) for row in rows)

    def unresolved(
        self, *, due_before: datetime, limit: int = 50
    ) -> tuple[Episode, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise MemoryError("Episode limit must be between 1 and 500.")
        pivot = _aware(due_before, label="due_before")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT e.episode_id, e.run_id, e.symbol, e.as_of, e.recorded_at, "
                "e.source, e.decision_source, e.model, e.regime, e.action, "
                "e.status, e.entry_price_paise, e.stop_loss_paise, "
                "e.target_price_paise, e.quantity, e.predicted_probability, "
                "e.detail_json FROM episodic_memory AS e "
                "LEFT JOIN episode_outcomes AS o ON o.episode_id = e.episode_id "
                "WHERE o.episode_id IS NULL AND e.as_of <= ? "
                "ORDER BY e.as_of ASC, e.episode_id LIMIT ?",
                (timestamp(pivot), limit),
            ).fetchall()
        return tuple(self._episode(row) for row in rows)

    def calibration(
        self, *, as_of: datetime | None = None
    ) -> tuple[CalibrationBucket, ...]:
        pivot = utc(self.clock.now()) if as_of is None else _aware(as_of, label="as_of")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT e.decision_source, e.model, e.predicted_probability, "
                "o.outcome FROM episodic_memory AS e "
                "JOIN episode_outcomes AS o ON o.episode_id = e.episode_id "
                "WHERE e.predicted_probability IS NOT NULL "
                "AND e.as_of <= ? AND o.resolved_at <= ? "
                "ORDER BY e.decision_source, e.model LIMIT 100000",
                (timestamp(pivot), timestamp(pivot)),
            ).fetchall()
        grouped: dict[tuple[str, str], list[tuple[float, int]]] = {}
        for source, model, probability, outcome in rows:
            try:
                value = float(str(probability))
            except ValueError:
                continue
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                continue
            grouped.setdefault((str(source), str(model)), []).append(
                (value, int(outcome))
            )
        buckets = [
            CalibrationBucket(
                decision_source=source,
                model=model,
                summary=summarize(pairs),
            )
            for (source, model), pairs in sorted(grouped.items())
        ]
        return tuple(bucket for bucket in buckets if bucket.summary.count > 0)

    @staticmethod
    def _detail_json(detail: Mapping[str, object] | None) -> str:
        payload = dict(detail or {})
        try:
            text = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise MemoryError("Episode detail must be JSON-serializable.") from exc
        if len(text) > MAX_DETAIL_CHARS:
            raise MemoryError("Episode detail is too large.")
        return text

    @staticmethod
    def _episode(row: tuple[object, ...]) -> Episode:
        detail_raw = str(row[16])
        detail = json.loads(detail_raw) if detail_raw else {}
        if not isinstance(detail, dict):
            raise MemoryError("Stored episode detail is invalid.")
        return Episode(
            episode_id=str(row[0]),
            run_id=None if row[1] is None else str(row[1]),
            symbol=str(row[2]),
            as_of=parse_timestamp(str(row[3])),
            recorded_at=parse_timestamp(str(row[4])),
            source=EpisodeSource(str(row[5])),
            decision_source=EpisodeDecisionSource(str(row[6])),
            model=str(row[7]),
            regime=None if row[8] is None else str(row[8]),
            action=EpisodeAction(str(row[9])),
            status=EpisodeStatus(str(row[10])),
            entry_price_paise=None if row[11] is None else int(str(row[11])),
            stop_loss_paise=None if row[12] is None else int(str(row[12])),
            target_price_paise=None if row[13] is None else int(str(row[13])),
            quantity=None if row[14] is None else int(str(row[14])),
            predicted_probability=(None if row[15] is None else float(str(row[15]))),
            detail=detail,
        )


__all__ = [
    "CalibrationBucket",
    "Episode",
    "EpisodeAction",
    "EpisodeDecisionSource",
    "EpisodeOutcome",
    "EpisodeSource",
    "EpisodeStatus",
    "EpisodicMemoryStore",
    "MemoryConflict",
    "MemoryError",
    "MissingEpisode",
]
