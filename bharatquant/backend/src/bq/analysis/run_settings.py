"""Immutable audit record of analysis runs.

Every run writes one row: the vendors that served the data, whether it was
real-time, the deterministic indicator snapshot hash, how many debate rounds ran,
the tokens spent, and the decision. The row cannot be updated or deleted; a
correction is a new run. That is the same discipline the audit chain and the
model-attempt ledger already follow.

The point is reviewability. Months later, "why did it say BUY on RELIANCE that
day?" must be answerable from the record without re-running a model.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bq.core import (
    Clock,
    SystemClock,
)
from bq.core import (
    canonical_digest as core_canonical_digest,
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
from bq.infra.audit import append_event


def _utc(value: datetime) -> datetime:
    try:
        return core_utc(value)
    except ValueError as exc:
        raise AnalysisRunError("Run timestamps must be timezone-aware.") from exc


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return core_parse_timestamp(value)


IDENTIFIER = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._:-]{0,127}")
MAX_PLAN_JSON = 8192
MAX_DETAIL_JSON = 65536


class AnalysisRunError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    """A stored run, as read back for the UI and the CLI."""

    run_id: str
    symbol: str
    timeframe: str
    as_of: datetime
    started_at: datetime
    finished_at: datetime
    vendor_quotes: str
    vendor_daily: str
    is_realtime: bool
    data_freshness: str
    bar_count: int
    facts_sha256: str
    composite: float | None
    debate_attempted: bool
    debate_rounds: int
    mode: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    signal: str
    plan_json: str | None
    plan_sha256: str | None
    vetoes: tuple[str, ...]
    detail: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "as_of": self.as_of.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "vendor_quotes": self.vendor_quotes,
            "vendor_daily": self.vendor_daily,
            "is_realtime": self.is_realtime,
            "data_freshness": self.data_freshness,
            "bar_count": self.bar_count,
            "facts_sha256": self.facts_sha256,
            "composite": self.composite,
            "debate_attempted": self.debate_attempted,
            "debate_rounds": self.debate_rounds,
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "signal": self.signal,
            "plan": json.loads(self.plan_json) if self.plan_json else None,
            "plan_sha256": self.plan_sha256,
            "vetoes": list(self.vetoes),
            "detail": self.detail,
        }


def _digest(payload: object) -> str:
    return core_canonical_digest(payload)


class AnalysisRunStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=True)

    def record(
        self,
        *,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        started_at: datetime,
        vendor_quotes: str,
        vendor_daily: str,
        is_realtime: bool,
        data_freshness: str,
        bar_count: int,
        facts: tuple[str, ...],
        composite: float | None,
        debate_attempted: bool,
        debate_rounds: int,
        mode: str,
        provider: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        signal: str,
        plan: dict[str, object] | None,
        vetoes: tuple[str, ...],
        detail: dict[str, object],
    ) -> AnalysisRun:
        if not isinstance(symbol, str) or not 1 <= len(symbol) <= 96:
            raise AnalysisRunError("A symbol is required.")
        if timeframe not in {"intraday", "swing"}:
            raise AnalysisRunError("Unknown timeframe.")
        if data_freshness not in {"realtime_kite", "delayed_yfinance"}:
            raise AnalysisRunError("Unknown data freshness label.")
        if mode not in {"FULL", "RULES_ONLY"}:
            raise AnalysisRunError("Unknown analysis mode.")
        if signal not in {"BUY", "SELL", "HOLD", "NONE"}:
            raise AnalysisRunError("Unknown signal.")
        if (
            isinstance(bar_count, bool)
            or not isinstance(bar_count, int)
            or bar_count < 0
        ):
            raise AnalysisRunError("Bar count must be a nonnegative integer.")
        if (
            isinstance(debate_rounds, bool)
            or not isinstance(debate_rounds, int)
            or debate_rounds < 0
        ):
            raise AnalysisRunError("Debate rounds must be a nonnegative integer.")
        for label, value in (("input", input_tokens), ("output", output_tokens)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise AnalysisRunError(f"{label} tokens must be null or nonnegative.")

        detail_json = json.dumps(detail, sort_keys=True, separators=(",", ":"))
        if len(detail_json) > MAX_DETAIL_JSON:
            raise AnalysisRunError("Run detail exceeded the size limit.")
        plan_json = (
            None
            if plan is None
            else json.dumps(plan, sort_keys=True, separators=(",", ":"))
        )
        if plan_json is not None and len(plan_json) > MAX_PLAN_JSON:
            raise AnalysisRunError("Plan exceeded the size limit.")

        finished_at = _utc(self.clock.now())
        run_id = secrets.token_urlsafe(18)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO analysis_runs "
                    "(run_id, symbol, timeframe, as_of, started_at, finished_at, "
                    "vendor_quotes, vendor_daily, is_realtime, data_freshness, "
                    "bar_count, facts_sha256, composite, debate_attempted, "
                    "debate_rounds, mode, provider, model, input_tokens, "
                    "output_tokens, signal, plan_json, plan_sha256, vetoes_json, "
                    "detail_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        symbol,
                        timeframe,
                        _timestamp(as_of),
                        _timestamp(started_at),
                        _timestamp(finished_at),
                        vendor_quotes[:64],
                        vendor_daily[:64],
                        1 if is_realtime else 0,
                        data_freshness,
                        bar_count,
                        _digest(list(facts)),
                        composite,
                        1 if debate_attempted else 0,
                        debate_rounds,
                        mode,
                        provider[:64],
                        model[:200],
                        input_tokens,
                        output_tokens,
                        signal,
                        plan_json,
                        None if plan_json is None else _digest(plan),
                        json.dumps(list(vetoes), sort_keys=True),
                        detail_json,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=finished_at,
                    actor="owner",
                    action="analysis.run.recorded",
                    object_type="analysis_run",
                    object_id=run_id,
                    payload={"symbol": symbol, "signal": signal, "mode": mode},
                )
        return self.get(run_id)

    def get(self, run_id: str) -> AnalysisRun:
        if not isinstance(run_id, str) or IDENTIFIER.fullmatch(run_id) is None:
            raise AnalysisRunError("Invalid run identifier.")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT run_id, symbol, timeframe, as_of, started_at, finished_at, "
                "vendor_quotes, vendor_daily, is_realtime, data_freshness, "
                "bar_count, facts_sha256, composite, debate_attempted, debate_rounds, "
                "mode, provider, model, input_tokens, output_tokens, signal, "
                "plan_json, plan_sha256, vetoes_json, detail_json "
                "FROM analysis_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise AnalysisRunError("Analysis run not found.")
        return self._row_to_run(row)

    def recent(
        self, limit: int = 20, *, symbol: str | None = None
    ) -> tuple[AnalysisRun, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise AnalysisRunError("Limit must be between 1 and 100.")
        query = (
            "SELECT run_id, symbol, timeframe, as_of, started_at, finished_at, "
            "vendor_quotes, vendor_daily, is_realtime, data_freshness, "
            "bar_count, facts_sha256, composite, debate_attempted, debate_rounds, "
            "mode, provider, model, input_tokens, output_tokens, signal, "
            "plan_json, plan_sha256, vetoes_json, detail_json "
            "FROM analysis_runs "
        )
        params: tuple[object, ...]
        if symbol is None:
            query += "ORDER BY finished_at DESC LIMIT ?"
            params = (limit,)
        else:
            query += "WHERE symbol = ? ORDER BY finished_at DESC LIMIT ?"
            params = (symbol, limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._row_to_run(row) for row in rows)

    @staticmethod
    def _row_to_run(row: tuple[object, ...]) -> AnalysisRun:
        return AnalysisRun(
            run_id=str(row[0]),
            symbol=str(row[1]),
            timeframe=str(row[2]),
            as_of=_parse_timestamp(str(row[3])),
            started_at=_parse_timestamp(str(row[4])),
            finished_at=_parse_timestamp(str(row[5])),
            vendor_quotes=str(row[6]),
            vendor_daily=str(row[7]),
            is_realtime=bool(row[8]),
            data_freshness=str(row[9]),
            bar_count=int(str(row[10])),
            facts_sha256=str(row[11]),
            composite=None if row[12] is None else float(str(row[12])),
            debate_attempted=bool(row[13]),
            debate_rounds=int(str(row[14])),
            mode=str(row[15]),
            provider=str(row[16]),
            model=str(row[17]),
            input_tokens=None if row[18] is None else int(str(row[18])),
            output_tokens=None if row[19] is None else int(str(row[19])),
            signal=str(row[20]),
            plan_json=None if row[21] is None else str(row[21]),
            plan_sha256=None if row[22] is None else str(row[22]),
            vetoes=tuple(json.loads(str(row[23]))),
            detail=json.loads(str(row[24])),
        )


__all__ = [
    "AnalysisRun",
    "AnalysisRunError",
    "AnalysisRunStore",
]
