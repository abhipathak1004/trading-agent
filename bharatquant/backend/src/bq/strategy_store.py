"""Append-only versioned strategies and backtest runs."""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bq.backtest import BacktestResult
from bq.core import (
    Clock,
    SystemClock,
    canonical_digest,
    open_connection,
    parse_timestamp,
    timestamp,
    utc,
)
from bq.infra.audit import append_event
from bq.strategies import StrategyError, StrategyKind, StrategyLifecycle
from bq.strategy_dsl import (
    EntryRule,
    ExitRule,
    StrategyDefinition,
    from_dict,
)

MAX_DEFINITION_CHARS = 50_000
MAX_TRADES = 5_000


@dataclass(frozen=True, slots=True)
class StrategyRecord:
    definition: StrategyDefinition
    lifecycle: StrategyLifecycle
    content_hash: str
    is_seed: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BacktestRun:
    run_id: str
    strategy_id: str
    version: str
    engine: str
    start: datetime
    end: datetime
    initial_capital_paise: int
    metrics: dict[str, object]
    trades: list[dict[str, object]]
    content_hash: str
    created_at: datetime
    review_decision: str | None = None
    review_note: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyReview:
    review_id: str
    run_id: str
    decision: str
    note: str
    created_at: datetime


class StrategyStoreError(ValueError):
    pass


class StrategyStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def save(
        self,
        definition: StrategyDefinition,
        *,
        lifecycle: StrategyLifecycle = StrategyLifecycle.DRAFT,
        is_seed: bool = False,
    ) -> StrategyRecord:
        if not isinstance(definition, StrategyDefinition):
            raise StrategyStoreError("A strategy definition is required.")
        if not isinstance(lifecycle, StrategyLifecycle):
            raise StrategyStoreError("Invalid strategy lifecycle.")
        if not isinstance(is_seed, bool):
            raise StrategyStoreError("Seed flag must be a boolean.")
        definition_json = definition.to_json()
        if len(definition_json) > MAX_DEFINITION_CHARS:
            raise StrategyStoreError("Strategy definition is too large.")
        content_hash = definition.content_hash
        now = utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT definition_json, content_hash, lifecycle "
                    "FROM strategy_versions WHERE strategy_id = ? AND version = ?",
                    (definition.strategy_id, definition.version),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing[1]) != content_hash
                        or str(existing[2]) != lifecycle.value
                    ):
                        raise StrategyStoreError(
                            "Strategy version already has conflicting content."
                        )
                    return self.get(definition.strategy_id, definition.version)
                connection.execute(
                    "INSERT INTO strategy_versions "
                    "(strategy_id, version, name, kind, lifecycle, definition_json, "
                    "content_hash, is_seed, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        definition.strategy_id,
                        definition.version,
                        definition.name,
                        definition.kind.value,
                        lifecycle.value,
                        definition_json,
                        content_hash,
                        1 if is_seed else 0,
                        timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="strategy.version.saved",
                    object_type="strategy",
                    object_id=f"{definition.strategy_id}:{definition.version}",
                    payload={
                        "content_hash": content_hash,
                        "is_seed": "true" if is_seed else "false",
                    },
                )
        return self.get(definition.strategy_id, definition.version)

    def get(self, strategy_id: str, version: str) -> StrategyRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT strategy_id, version, lifecycle, definition_json, "
                "content_hash, is_seed, created_at FROM strategy_versions "
                "WHERE strategy_id = ? AND version = ?",
                (strategy_id, version),
            ).fetchone()
        if row is None:
            raise StrategyStoreError("Strategy version not found.")
        return self._row(row)

    def list(self, *, limit: int = 100) -> tuple[StrategyRecord, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 250
        ):
            raise StrategyStoreError("Strategy limit must be between 1 and 250.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT strategy_id, version, lifecycle, definition_json, "
                "content_hash, is_seed, created_at FROM strategy_versions "
                "ORDER BY created_at DESC, strategy_id, version LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def ensure_seeds(self) -> None:
        for definition in seed_strategies():
            try:
                self.get(definition.strategy_id, definition.version)
            except StrategyStoreError:
                self.save(definition, lifecycle=StrategyLifecycle.DRAFT, is_seed=True)

    @staticmethod
    def _row(row: tuple[object, ...]) -> StrategyRecord:
        try:
            definition = from_dict(json.loads(str(row[3])))
        except (json.JSONDecodeError, StrategyError) as exc:
            raise StrategyStoreError("Stored strategy definition is invalid.") from exc
        return StrategyRecord(
            definition=definition,
            lifecycle=StrategyLifecycle(str(row[2])),
            content_hash=str(row[4]),
            is_seed=bool(row[5]),
            created_at=parse_timestamp(str(row[6])),
        )


class BacktestStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def record(self, result: BacktestResult) -> BacktestRun:
        metrics = result.metrics.to_dict()
        trades = [item.to_dict() for item in result.trades]
        if len(trades) > MAX_TRADES:
            raise StrategyStoreError("Backtest produced too many trades to archive.")
        payload: dict[str, object] = {
            "strategy_id": result.strategy_id,
            "version": result.version,
            "engine": result.engine.value,
            "start": result.start.isoformat(),
            "end": result.end.isoformat(),
            "initial_capital_paise": result.initial_capital_paise,
            "metrics": metrics,
            "trades": trades,
        }
        content_hash = canonical_digest(payload)
        now = utc(self.clock.now())
        run_id = "backtest_" + secrets.token_urlsafe(18)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO backtest_runs "
                    "(run_id, strategy_id, version, engine, start_at, end_at, "
                    "initial_capital_paise, metrics_json, trades_json, content_hash, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        result.strategy_id,
                        result.version,
                        result.engine.value,
                        timestamp(result.start),
                        timestamp(result.end),
                        result.initial_capital_paise,
                        json.dumps(metrics, sort_keys=True, separators=(",", ":")),
                        json.dumps(trades, sort_keys=True, separators=(",", ":")),
                        content_hash,
                        timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="system",
                    action="backtest.recorded",
                    object_type="backtest_run",
                    object_id=run_id,
                    payload={"content_hash": content_hash},
                )
        return self.get(run_id)

    def get(self, run_id: str) -> BacktestRun:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT run_id, strategy_id, version, engine, start_at, end_at, "
                "initial_capital_paise, metrics_json, trades_json, content_hash, "
                "created_at FROM backtest_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise StrategyStoreError("Backtest run not found.")
            review = connection.execute(
                "SELECT decision, note FROM strategy_reviews WHERE run_id = ? "
                "ORDER BY created_at DESC, review_id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._row(row, review)

    def list(
        self, *, limit: int = 30, strategy_id: str | None = None
    ) -> tuple[BacktestRun, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise StrategyStoreError("Backtest limit must be between 1 and 100.")
        with closing(self._connect()) as connection:
            if strategy_id is None:
                rows = connection.execute(
                    "SELECT run_id FROM backtest_runs "
                    "ORDER BY created_at DESC, run_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT run_id FROM backtest_runs WHERE strategy_id = ? "
                    "ORDER BY created_at DESC, run_id DESC LIMIT ?",
                    (strategy_id, limit),
                ).fetchall()
        return tuple(self.get(str(row[0])) for row in rows)

    def review(self, run_id: str, *, decision: str, note: str) -> StrategyReview:
        if decision not in {"ACCEPTED", "REJECTED"}:
            raise StrategyStoreError("Review decision must be ACCEPTED or REJECTED.")
        if not isinstance(note, str) or len(note) > 512:
            raise StrategyStoreError("Review note is too long.")
        self.get(run_id)
        now = utc(self.clock.now())
        review_id = secrets.token_urlsafe(18)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO strategy_reviews "
                    "(review_id, run_id, decision, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (review_id, run_id, decision, note, timestamp(now)),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="strategy.backtest_reviewed",
                    object_type="backtest_run",
                    object_id=run_id,
                    payload={"decision": decision},
                )
        return StrategyReview(
            review_id=review_id,
            run_id=run_id,
            decision=decision,
            note=note,
            created_at=now,
        )

    @staticmethod
    def _row(row: tuple[object, ...], review: tuple[object, ...] | None) -> BacktestRun:
        return BacktestRun(
            run_id=str(row[0]),
            strategy_id=str(row[1]),
            version=str(row[2]),
            engine=str(row[3]),
            start=parse_timestamp(str(row[4])),
            end=parse_timestamp(str(row[5])),
            initial_capital_paise=int(str(row[6])),
            metrics=json.loads(str(row[7])),
            trades=json.loads(str(row[8])),
            content_hash=str(row[9]),
            created_at=parse_timestamp(str(row[10])),
            review_decision=str(review[0]) if review is not None else None,
            review_note=str(review[1]) if review is not None else None,
        )


def seed_strategies() -> tuple[StrategyDefinition, ...]:
    return (
        StrategyDefinition(
            strategy_id="seed-momentum-20",
            version="1",
            name="Momentum 20",
            kind=StrategyKind.RULE,
            timeframe="swing",
            universe=("NSE:RELIANCE", "NSE:TCS"),
            entry_rule=EntryRule.SMA_CROSS,
            exit_rule=ExitRule.SMA_CROSS_DOWN,
            lookback=20,
            atr_period=14,
            stop_atr_multiple=1.5,
            target_atr_multiple=2.5,
            risk_per_trade_bps=100,
            slippage_bps=5,
            max_positions=2,
        ),
        StrategyDefinition(
            strategy_id="seed-breakout-20",
            version="1",
            name="Donchian breakout 20",
            kind=StrategyKind.RULE,
            timeframe="swing",
            universe=("NSE:RELIANCE", "NSE:TCS"),
            entry_rule=EntryRule.DONCHIAN_BREAKOUT,
            exit_rule=ExitRule.DONCHIAN_EXIT,
            lookback=20,
            atr_period=14,
            stop_atr_multiple=1.5,
            target_atr_multiple=3.0,
            risk_per_trade_bps=100,
            slippage_bps=5,
            max_positions=2,
        ),
    )


__all__ = [
    "BacktestRun",
    "BacktestStore",
    "StrategyRecord",
    "StrategyReview",
    "StrategyStore",
    "StrategyStoreError",
    "seed_strategies",
]
