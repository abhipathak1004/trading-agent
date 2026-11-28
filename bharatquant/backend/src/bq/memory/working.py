"""Append-only working memory for one analysed symbol in a run.

Working memory is the typed state the coordinator had while it was deciding:
routing, specialists, evidence, debate, final gate and the proposed plan. It is
not a second analysis ledger and it excludes raw quotes, OHLCV candles, positions
and margins. A Strategy run contains several symbols, so the key is
``(run_id, symbol)``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from bq.analysis.pipeline import AnalysisResult

SCHEMA_VERSION = 1
RUN_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._:-]{0,127}")
MAX_STATE_JSON = 65_536
MAX_ROUTING_ENTRIES = 64
MAX_SPECIALISTS = 16
MAX_NOTES = 32


class WorkingMemoryError(ValueError):
    pass


class WorkingMemoryConflict(WorkingMemoryError):
    pass


class MissingWorkingMemory(WorkingMemoryError):
    pass


@dataclass(frozen=True, slots=True)
class WorkingState:
    run_id: str
    symbol: str
    source: str
    as_of: datetime
    timeframe: str
    mode: str
    data_provenance: dict[str, object]
    technical: dict[str, object]
    routing: tuple[dict[str, object], ...]
    specialists: tuple[dict[str, object], ...]
    evidence: dict[str, object] | None
    debate: dict[str, object]
    final_gate: dict[str, object] | None
    confidence: dict[str, object] | None
    plan: dict[str, object] | None
    vetoes: tuple[str, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "symbol": self.symbol,
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "timeframe": self.timeframe,
            "mode": self.mode,
            "data_provenance": self.data_provenance,
            "technical": self.technical,
            "routing": list(self.routing),
            "specialists": list(self.specialists),
            "evidence": self.evidence,
            "debate": self.debate,
            "final_gate": self.final_gate,
            "confidence": self.confidence,
            "plan": self.plan,
            "vetoes": list(self.vetoes),
            "notes": list(self.notes),
        }

    @classmethod
    def from_result(
        cls, result: AnalysisResult, *, run_id: str, source: str
    ) -> WorkingState:
        report = result.report.to_dict()
        technical = {
            "bar_count": result.report.bar_count,
            "composite": result.report.composite,
            "sub_scores": report.get("sub_scores"),
            "facts": list(result.report.facts())[:32],
        }
        debate: dict[str, object] = {
            "bull": None if result.bull is None else result.bull.to_dict(),
            "bear": None if result.bear is None else result.bear.to_dict(),
            "verdict": None if result.verdict is None else result.verdict.to_dict(),
            "risk": None if result.risk is None else result.risk.to_dict(),
        }
        return cls(
            run_id=run_id,
            symbol=result.symbol,
            source=source,
            as_of=result.as_of,
            timeframe=result.timeframe,
            mode=result.mode,
            data_provenance={
                "vendor_daily": result.vendor_daily,
                "vendor_quotes": result.vendor_quotes,
                "is_realtime": result.is_realtime,
                "data_freshness": (
                    "realtime_kite" if result.is_realtime else "delayed_yfinance"
                ),
            },
            technical=technical,
            routing=tuple(
                use.to_dict() for use in result.model_routing[:MAX_ROUTING_ENTRIES]
            ),
            specialists=tuple(
                brief.to_dict() for brief in result.specialists[:MAX_SPECIALISTS]
            ),
            evidence=None if result.evidence is None else result.evidence.to_dict(),
            debate=debate,
            final_gate=(
                None if result.final_gate is None else result.final_gate.to_dict()
            ),
            confidence=(
                None if result.confidence is None else result.confidence.to_dict()
            ),
            plan=None if result.plan is None else result.plan.to_dict(),
            vetoes=result.vetoes,
            notes=tuple(note[:500] for note in result.notes[:MAX_NOTES]),
        )


@dataclass(frozen=True, slots=True)
class WorkingMemoryRecord:
    run_id: str
    symbol: str
    source: str
    as_of: datetime
    schema_version: int
    state: dict[str, object]
    state_hash: str
    recorded_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "symbol": self.symbol,
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "schema_version": self.schema_version,
            "state": self.state,
            "state_hash": self.state_hash,
            "recorded_at": self.recorded_at.isoformat(),
        }


class WorkingMemoryStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def record(
        self,
        result: AnalysisResult,
        *,
        run_id: str,
        source: str,
    ) -> WorkingMemoryRecord:
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            raise WorkingMemoryError("Invalid working-memory run identifier.")
        if source not in {"analysis", "strategy"}:
            raise WorkingMemoryError("Unknown working-memory source.")
        state = WorkingState.from_result(result, run_id=run_id, source=source)
        payload = state.to_dict()
        state_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(state_json) > MAX_STATE_JSON:
            raise WorkingMemoryError("Working memory exceeded the size limit.")
        state_hash = canonical_digest(payload)
        recorded_at = utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT state_hash FROM working_memory "
                    "WHERE run_id = ? AND symbol = ?",
                    (run_id, state.symbol),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != state_hash:
                        raise WorkingMemoryConflict(
                            "Working memory already exists with different content."
                        )
                    return self._get_one(connection, run_id, state.symbol)
                connection.execute(
                    "INSERT INTO working_memory "
                    "(run_id, symbol, source, as_of, schema_version, state_json, "
                    "state_hash, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        state.symbol,
                        source,
                        timestamp(state.as_of),
                        SCHEMA_VERSION,
                        state_json,
                        state_hash,
                        timestamp(recorded_at),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=recorded_at,
                    actor="system",
                    action="memory.working.recorded",
                    object_type="working_memory",
                    object_id=f"{run_id}:{state.symbol}"[:128],
                    payload={"symbol": state.symbol, "state_hash": state_hash},
                )
                return self._get_one(connection, run_id, state.symbol)

    def get(self, run_id: str) -> tuple[WorkingMemoryRecord, ...]:
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            raise WorkingMemoryError("Invalid working-memory run identifier.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT run_id, symbol FROM working_memory "
                "WHERE run_id = ? ORDER BY symbol",
                (run_id,),
            ).fetchall()
            if not rows:
                raise MissingWorkingMemory("No working memory for this run.")
            return tuple(
                self._get_one(connection, str(row[0]), str(row[1])) for row in rows
            )

    def _get_one(
        self, connection: sqlite3.Connection, run_id: str, symbol: str
    ) -> WorkingMemoryRecord:
        row = connection.execute(
            "SELECT run_id, symbol, source, as_of, schema_version, state_json, "
            "state_hash, recorded_at FROM working_memory "
            "WHERE run_id = ? AND symbol = ?",
            (run_id, symbol),
        ).fetchone()
        if row is None:
            raise MissingWorkingMemory("No working memory for this run.")
        return WorkingMemoryRecord(
            run_id=str(row[0]),
            symbol=str(row[1]),
            source=str(row[2]),
            as_of=parse_timestamp(str(row[3])),
            schema_version=int(row[4]),
            state=json.loads(str(row[5])),
            state_hash=str(row[6]),
            recorded_at=parse_timestamp(str(row[7])),
        )


__all__ = [
    "MAX_STATE_JSON",
    "MissingWorkingMemory",
    "SCHEMA_VERSION",
    "WorkingMemoryConflict",
    "WorkingMemoryError",
    "WorkingMemoryRecord",
    "WorkingMemoryStore",
    "WorkingState",
]
