"""SQLite persistence for model eval runs and their per-model scorecards.

Runs are append-only; only the lifecycle timestamps/status change. The partial
unique index ``eval_runs_single_active`` is the real single-run lock, so starting
a second benchmark fails inside the transaction instead of racing the API.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bq.core import (
    Clock,
    SystemClock,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    parse_timestamp as core_parse_timestamp,
)
from bq.core import (
    timestamp as core_timestamp,
)
from bq.core import (
    utc as core_utc,
)
from bq.evals.scoring import EvalCase, load_cases
from bq.infra.audit import append_event

ACTIVE_STATUSES = frozenset({"queued", "running"})
RUN_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})


class EvalRunNotFound(LookupError):
    """No eval run exists for the requested id."""


class EvalRunActive(RuntimeError):
    """Another eval run is queued or running; only one may be active at a time."""


def _utc(value: datetime) -> datetime:
    try:
        return core_utc(value)
    except ValueError as exc:
        raise ValueError("Eval timestamps must be timezone-aware.") from exc


def _timestamp(value: datetime) -> str:
    return core_timestamp(_utc(value))


def _parse_timestamp(value: str | None) -> datetime | None:
    return None if value is None else core_parse_timestamp(value)


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class EvalRun:
    id: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    status: str
    models: tuple[str, ...]
    categories: tuple[str, ...]
    config_path: str
    results_json_path: str | None
    error_message: str | None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


@dataclass(frozen=True, slots=True)
class EvalModelScore:
    model: str
    total_cases: int
    overall_pass_rate: float
    accuracy_pass_rate: float
    json_parse_rate: float | None
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    brier_score: float | None
    ece_5bin: float | None
    brier_n: int
    category_rates: dict[str, float | None] = field(default_factory=dict)


class EvalStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=True)

    # -- lifecycle --------------------------------------------------------

    def create_run(
        self,
        *,
        models: tuple[str, ...],
        categories: tuple[str, ...],
        config_path: str,
    ) -> EvalRun:
        if not models:
            raise ValueError("At least one model is required.")
        run_id = secrets.token_urlsafe(18)
        now = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                active = connection.execute(
                    "SELECT id FROM eval_runs WHERE status IN ('queued', 'running') "
                    "LIMIT 1"
                ).fetchone()
                if active is not None:
                    raise EvalRunActive(f"Eval run {active[0]} is already active.")
                connection.execute(
                    "INSERT INTO eval_runs "
                    "(id, created_at, status, models_json, categories_json, config_path) "
                    "VALUES (?, ?, 'queued', ?, ?, ?)",
                    (
                        run_id,
                        _timestamp(now),
                        _dumps(list(models)),
                        _dumps(list(categories)),
                        config_path,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="evals.run.created",
                    object_type="eval_run",
                    object_id=run_id,
                    payload={"models": ",".join(models)},
                )
        return self.get_run(run_id)

    def mark_started(self, run_id: str) -> EvalRun:
        return self._transition(run_id, "running", from_statuses={"queued"})

    def mark_completed(self, run_id: str, results_json_path: str) -> EvalRun:
        return self._transition(
            run_id,
            "completed",
            from_statuses={"running"},
            results_json_path=results_json_path,
        )

    def mark_failed(self, run_id: str, error_message: str) -> EvalRun:
        return self._transition(
            run_id,
            "failed",
            from_statuses={"queued", "running"},
            error_message=error_message[:2000],
        )

    def mark_cancelled(self, run_id: str) -> EvalRun:
        return self._transition(
            run_id, "cancelled", from_statuses={"queued", "running"}
        )

    def set_config_path(self, run_id: str, config_path: str) -> EvalRun:
        """Point a queued run at its generated Promptfoo config."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                updated = connection.execute(
                    "UPDATE eval_runs SET config_path = ? WHERE id = ?",
                    (config_path, run_id),
                )
                if not updated.rowcount:
                    raise EvalRunNotFound(f"Eval run {run_id} does not exist.")
        return self.get_run(run_id)

    def _transition(
        self,
        run_id: str,
        status: str,
        *,
        from_statuses: set[str],
        results_json_path: str | None = None,
        error_message: str | None = None,
    ) -> EvalRun:
        if status not in RUN_STATUSES:
            raise ValueError(f"Unknown eval status {status!r}.")
        now = _utc(self.clock.now())
        terminal = status in {"completed", "failed", "cancelled"}
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                row = connection.execute(
                    "SELECT status FROM eval_runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise EvalRunNotFound(f"Eval run {run_id} does not exist.")
                if str(row[0]) not in from_statuses:
                    raise EvalRunActive(
                        f"Eval run {run_id} is {row[0]}, cannot become {status}."
                    )
                started = _timestamp(now) if status == "running" else None
                connection.execute(
                    "UPDATE eval_runs SET status = ?, "
                    "started_at = COALESCE(started_at, ?), "
                    "completed_at = ?, "
                    "results_json_path = COALESCE(?, results_json_path), "
                    "error_message = ? "
                    "WHERE id = ?",
                    (
                        status,
                        started,
                        _timestamp(now) if terminal else None,
                        results_json_path,
                        error_message,
                        run_id,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action=f"evals.run.{status}",
                    object_type="eval_run",
                    object_id=run_id,
                    payload={"status": status},
                )
        return self.get_run(run_id)

    # -- reads ------------------------------------------------------------

    def _row_to_run(self, row: tuple[Any, ...]) -> EvalRun:
        return EvalRun(
            id=str(row[0]),
            created_at=_parse_timestamp(str(row[1])) or core_utc(self.clock.now()),
            started_at=_parse_timestamp(row[2] if row[2] is None else str(row[2])),
            completed_at=_parse_timestamp(row[3] if row[3] is None else str(row[3])),
            status=str(row[4]),
            models=tuple(json.loads(str(row[5]))),
            categories=tuple(json.loads(str(row[6]))),
            config_path=str(row[7]),
            results_json_path=None if row[8] is None else str(row[8]),
            error_message=None if row[9] is None else str(row[9]),
        )

    _RUN_COLUMNS = (
        "id, created_at, started_at, completed_at, status, models_json, "
        "categories_json, config_path, results_json_path, error_message"
    )

    def get_run(self, run_id: str) -> EvalRun:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT {self._RUN_COLUMNS} FROM eval_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise EvalRunNotFound(f"Eval run {run_id} does not exist.")
        return self._row_to_run(row)

    def list_runs(self, *, limit: int = 50) -> list[EvalRun]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {self._RUN_COLUMNS} FROM eval_runs "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def active_run(self) -> EvalRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT {self._RUN_COLUMNS} FROM eval_runs "
                "WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
        return None if row is None else self._row_to_run(row)

    # -- scores -----------------------------------------------------------

    def save_scores(
        self, run_id: str, summaries: list[dict[str, Any]]
    ) -> list[EvalModelScore]:
        self.get_run(run_id)
        now = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                for summary in summaries:
                    connection.execute(
                        "INSERT INTO eval_model_scores "
                        "(id, run_id, model, total_cases, overall_pass_rate, "
                        "accuracy_pass_rate, json_parse_rate, p50_ms, p95_ms, max_ms, "
                        "brier_score, ece_5bin, brier_n, category_rates_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(run_id, model) DO UPDATE SET "
                        "total_cases = excluded.total_cases, "
                        "overall_pass_rate = excluded.overall_pass_rate, "
                        "accuracy_pass_rate = excluded.accuracy_pass_rate, "
                        "json_parse_rate = excluded.json_parse_rate, "
                        "p50_ms = excluded.p50_ms, p95_ms = excluded.p95_ms, "
                        "max_ms = excluded.max_ms, brier_score = excluded.brier_score, "
                        "ece_5bin = excluded.ece_5bin, brier_n = excluded.brier_n, "
                        "category_rates_json = excluded.category_rates_json",
                        (
                            secrets.token_urlsafe(18),
                            run_id,
                            str(summary["model"]),
                            int(summary["total"]),
                            float(summary["overall_pass_rate"]),
                            float(summary["accuracy_pass_rate"]),
                            summary.get("json_parse_rate"),
                            summary.get("p50_ms"),
                            summary.get("p95_ms"),
                            summary.get("max_ms"),
                            summary.get("brier_score"),
                            summary.get("ece_5bin"),
                            int(summary.get("brier_n") or 0),
                            _dumps(summary.get("cat_rates") or {}),
                        ),
                    )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="evals.run.scored",
                    object_type="eval_run",
                    object_id=run_id,
                    payload={"models": str(len(summaries))},
                )
        return self.get_scores(run_id)

    def get_scores(self, run_id: str) -> list[EvalModelScore]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT model, total_cases, overall_pass_rate, accuracy_pass_rate, "
                "json_parse_rate, p50_ms, p95_ms, max_ms, brier_score, ece_5bin, "
                "brier_n, category_rates_json FROM eval_model_scores "
                "WHERE run_id = ? ORDER BY overall_pass_rate DESC, model ASC",
                (run_id,),
            ).fetchall()
        return [
            EvalModelScore(
                model=str(row[0]),
                total_cases=int(row[1]),
                overall_pass_rate=float(row[2]),
                accuracy_pass_rate=float(row[3]),
                json_parse_rate=None if row[4] is None else float(row[4]),
                p50_ms=None if row[5] is None else float(row[5]),
                p95_ms=None if row[6] is None else float(row[6]),
                max_ms=None if row[7] is None else float(row[7]),
                brier_score=None if row[8] is None else float(row[8]),
                ece_5bin=None if row[9] is None else float(row[9]),
                brier_n=int(row[10]),
                category_rates=json.loads(str(row[11])),
            )
            for row in rows
        ]

    # -- cases ------------------------------------------------------------

    def artifact_dir(self, run_id: str) -> Path:
        """Where a run's Promptfoo artifacts (results.json, scorecard.csv) live."""
        return self.path.parent / "evals" / run_id

    def read_cases(
        self,
        run_id: str,
        *,
        scorer_path: Path,
        model: str | None = None,
        category: str | None = None,
        status: str = "all",
    ) -> list[EvalCase]:
        """Filter a completed run's graded cases for the inspector drawer."""
        if status not in {"all", "failed", "passed"}:
            raise ValueError("Case status must be all, failed or passed.")
        run = self.get_run(run_id)
        if run.results_json_path is None:
            raise EvalRunNotFound(
                f"Eval run {run_id} has no results yet (status {run.status})."
            )
        cases = load_cases(scorer_path, Path(run.results_json_path))
        if model is not None:
            cases = [case for case in cases if case.model == model]
        if category is not None:
            cases = [case for case in cases if case.category == category]
        if status == "failed":
            cases = [case for case in cases if not case.passed]
        elif status == "passed":
            cases = [case for case in cases if case.passed]
        return cases
