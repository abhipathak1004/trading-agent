"""``/api/v1/research/*`` — the Research Lab HTTP surface.

Registered from ``create_app`` the way ``aihf`` is: a ``register(app, deps)``
called before the SPA catch-all, so it obeys the same rules as every neighbour —
``read_auth`` on reads, ``require_write`` on writes, and nothing mounted after the
static handler.

Two design points worth stating:

* **The stream is a journal.** ``events.jsonl`` is replayed from ``after=<seq>``,
  so a reconnect never re-delivers an event and a slow reader never skips one.
* **No ``CANCELLED`` job status.** Runs are cancelled by the in-process runner and
  recorded as ``cancelled`` on ``research_runs``; the shared ``job_runs`` lease
  contract is untouched.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from bq.backtest import run_backtest
from bq.core import parse_timestamp
from bq.paper_charges import IndiaCostModel
from bq.research_lab.alpha_bench import AlphaBenchError, alpha_bench
from bq.research_lab.contracts import MarketDataFrameContract
from bq.research_lab.evidence import EvidenceError, EvidenceLedger
from bq.research_lab.freeze import FrozenManifestError, freeze_run_inputs
from bq.research_lab.harness import (
    HarnessError,
    HarnessOutcome,
    ModelTurn,
    ResearchHarness,
    ResearchTool,
)
from bq.research_lab.reconcile import ReconcileError, paper_trade_reconcile
from bq.research_lab.replay import (
    ReplayError,
    WalkForwardSpec,
    bars_from_frame,
    folds_csv,
    replay_from_frozen,
)
from bq.research_lab.risk_xray import RiskXRayError, drawdown_episodes
from bq.research_lab.runner import Emit, ResearchBusy, ResearchRunner
from bq.research_lab.store import (
    ResearchArtifactRecord,
    ResearchArtifactStore,
    ResearchLabError,
    ResearchRunRecord,
    ResearchRunStore,
    ResearchSessionRecord,
    ResearchSessionStore,
)
from bq.settings import (
    SettingsError,
    SettingsStore,
    parse_research_lab,
    research_lab_payload,
    resolve_research_lab_models,
)
from bq.strategies import StrategyError
from bq.strategy_dsl import from_dict as strategy_from_dict

SSE_POLL_SECONDS = 1.0
SSE_HEARTBEAT_SECONDS = 15.0
SSE_MAX_SECONDS = 60 * 60
MAX_INLINE_ROWS = 20_000


class SessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    model_overrides: dict[str, dict[str, Any]] | None = None


class MessageCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)


class ReplayRequest(BaseModel):
    folds: int | None = Field(default=None, ge=2, le=20)
    holdout_bars: int | None = Field(default=None, ge=0, le=500)


class AlphaBenchRequest(BaseModel):
    bars: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_INLINE_ROWS)
    factor: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_INLINE_ROWS)
    horizons: list[int] = Field(default_factory=lambda: [1, 5, 20])
    source: str = "inline"
    membership_is_current: bool = True
    missing_symbols: list[str] = Field(default_factory=list)


class ReconcileRequest(BaseModel):
    initial_cash_paise: int = Field(gt=0)
    fills: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)
    ending_prices: dict[str, int] = Field(default_factory=dict)
    reported_state: dict[str, Any] = Field(default_factory=dict)
    tolerance_paise: int = Field(default=1, ge=0)


class ArtifactCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    body_md: str = Field(min_length=1, max_length=200_000)
    linked_run_ids: list[str] = Field(default_factory=list, max_length=200)
    evidence_refs: list[str] = Field(default_factory=list, max_length=200)


class ModelsUpdate(BaseModel):
    research_lab: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResearchDeps:
    database_path: Path
    artifacts_root: Path
    settings: SettingsStore
    read_auth: Callable[[Request], None]
    require_write: Callable[[Request], None]
    runner: ResearchRunner
    model_turn_factory: Callable[[], ModelTurn] | None = None


def _sse(payload: Mapping[str, object]) -> bytes:
    return f"data: {json.dumps(dict(payload), sort_keys=True, allow_nan=False)}\n\n".encode()


def _error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.get("ts")
        if isinstance(raw, str):
            item["ts"] = parse_timestamp(raw)
        cleaned.append(item)
    return pl.DataFrame(cleaned, schema_overrides={"ts": pl.Datetime("us", "UTC")})


def _contract(
    rows: list[dict[str, Any]], universe: tuple[str, ...]
) -> MarketDataFrameContract:
    frame = _frame(rows)
    delivered = tuple(
        sorted({str(item) for item in frame["symbol"].unique().to_list()})
    )
    return MarketDataFrameContract(
        frame=frame,
        source="inline",
        universe=universe,
        price_adjustment="raw",
        as_of_utc=datetime.now(UTC),
        bar_interval="1d",
        missing_symbols=tuple(sorted(set(universe) - set(delivered))),
    )


def _numbers(metrics: Mapping[str, object]) -> dict[str, float]:
    evidence: dict[str, float] = {}
    for key in ("sharpe", "sortino", "profit_factor", "win_rate", "return_bps"):
        raw = metrics.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = float(raw)
            if value == value and value not in (float("inf"), float("-inf")):
                evidence[key] = value
    return evidence


def _backtest_tool(run_id: str, run_root: Path) -> ResearchTool:
    def run(arguments: Mapping[str, object]) -> Mapping[str, object]:
        raw_strategy = arguments.get("strategy")
        raw_bars = arguments.get("bars")
        if not isinstance(raw_strategy, Mapping) or not isinstance(raw_bars, list):
            raise HarnessError("The backtest tool needs `strategy` and `bars`.")
        try:
            definition = strategy_from_dict(raw_strategy)
        except (StrategyError, ValueError) as exc:
            raise HarnessError(f"Invalid strategy: {exc}") from exc
        start = parse_timestamp(str(arguments["start"]))
        end = parse_timestamp(str(arguments["end"]))
        capital = int(str(arguments.get("initial_capital_paise", 1_000_000)))
        contract = _contract([dict(item) for item in raw_bars], definition.universe)
        path, digest = freeze_run_inputs(
            contract.sorted_frame(),
            {
                "strategy": dict(raw_strategy),
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            run_root,
        )
        result = run_backtest(
            definition,
            bars_from_frame(pl.read_parquet(str(path))),
            start=start,
            end=end,
            initial_capital_paise=capital,
            charge_schedule=IndiaCostModel().schedule,
        )
        metrics = result.metrics.to_dict()
        (run_root / "trades.json").write_text(
            json.dumps([trade.to_dict() for trade in result.trades], indent=2) + "\n",
            encoding="utf-8",
        )
        (run_root / "equity.json").write_text(
            json.dumps(
                {"equity": [str(item) for item in result.equity_curve]}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        risk: dict[str, object] = {}
        if len(result.equity_curve) >= 2:
            try:
                risk = drawdown_episodes([float(item) for item in result.equity_curve])
            except RiskXRayError:
                risk = {}
        (run_root / "risk_xray.json").write_text(
            json.dumps(risk, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        evidence = _numbers(metrics)
        units: dict[str, tuple[str, str]] = {
            key: ("ratio", "point") for key in evidence
        }
        if "return_bps" in evidence:
            units["return_bps"] = ("bp", "point")
        evidence["trades"] = float(str(metrics["trades"]))
        units["trades"] = ("count", "point")
        return {
            "summary": (
                f"backtest {result.strategy_id}: {metrics['trades']} trades, "
                f"return_bps {metrics['return_bps']}"
            ),
            "evidence": evidence,
            "units": units,
            "run_id": run_id,
            "source_ref": digest,
            "artifact_refs": ["inputs.parquet", "trades.json", "risk_xray.json"],
        }

    return ResearchTool(name="backtest", summary="Run a frozen backtest.", run=run)


def _alpha_tool(run_id: str) -> ResearchTool:
    def run(arguments: Mapping[str, object]) -> Mapping[str, object]:
        raw_bars = arguments.get("bars")
        raw_factor = arguments.get("factor")
        if not isinstance(raw_bars, list) or not isinstance(raw_factor, list):
            raise HarnessError("The alpha bench needs `bars` and `factor`.")
        horizons = arguments.get("horizons", [1, 5, 20])
        if not isinstance(horizons, (list, tuple)):
            raise HarnessError("Horizons must be a list.")
        result = alpha_bench(
            _frame([dict(item) for item in raw_bars]),
            _frame([dict(item) for item in raw_factor]),
            horizons=tuple(int(str(item)) for item in horizons),
            source=str(arguments.get("source", "inline")),
            membership_is_current=bool(arguments.get("membership_is_current", True)),
        )
        evidence: dict[str, float] = {}
        units: dict[str, tuple[str, str]] = {}
        rows = result["horizons"]
        if not isinstance(rows, list):
            raise HarnessError("The bench returned no horizons.")
        for row in rows:
            if not isinstance(row, dict):
                continue
            horizon = int(str(row["horizon_bars"]))
            for key in ("ic_mean", "rank_ic_mean", "icir", "turnover"):
                value = row.get(key)
                if isinstance(value, float) and value == value:
                    evidence[f"{key}_{horizon}"] = value
                    units[f"{key}_{horizon}"] = ("ratio", "point")
        meta = result["_meta"]
        if not isinstance(meta, dict):
            meta = {}
        return {
            "summary": (
                f"alpha bench on {meta.get('universe_size')} names; "
                f"survivorship {meta.get('survivorship_bias_warning')}"
            ),
            "evidence": evidence,
            "units": units,
            "run_id": run_id,
            "source_ref": "alpha_bench",
            "artifact_refs": [],
        }

    return ResearchTool(
        name="alpha_bench", summary="Benchmark a factor cross-sectionally.", run=run
    )


def register(app: FastAPI, deps: ResearchDeps) -> None:
    sessions = ResearchSessionStore(deps.database_path)
    runs = ResearchRunStore(deps.database_path)
    artifacts = ResearchArtifactStore(deps.database_path)
    ledger = EvidenceLedger(deps.database_path)
    runner = deps.runner

    def _session(session_id: str) -> ResearchSessionRecord:
        try:
            return sessions.get(session_id)
        except ResearchLabError as exc:
            raise _error(404, str(exc)) from exc

    def _run(run_id: str) -> ResearchRunRecord:
        try:
            return runs.get(run_id)
        except ResearchLabError as exc:
            raise _error(404, str(exc)) from exc

    def _root(record: ResearchRunRecord) -> Path:
        return (
            Path(record.artifact_dir)
            if record.artifact_dir
            else runner.run_dir(record.run_id)
        )

    # ---- sessions --------------------------------------------------------

    @app.post("/api/v1/research/sessions", status_code=201)
    def create_session(request: Request, body: SessionCreate) -> dict[str, object]:
        deps.require_write(request)
        if body.model_overrides is not None:
            try:
                resolve_research_lab_models(deps.settings.load(), body.model_overrides)
            except SettingsError as exc:
                raise _error(422, str(exc)) from exc
        return sessions.create(
            title=body.title, model_overrides=body.model_overrides
        ).to_dict()

    @app.get("/api/v1/research/sessions")
    def list_sessions(request: Request) -> dict[str, object]:
        deps.read_auth(request)
        return {"sessions": [item.to_dict() for item in sessions.list()]}

    @app.get("/api/v1/research/sessions/{session_id}")
    def get_session(request: Request, session_id: str) -> dict[str, object]:
        deps.read_auth(request)
        return {
            "session": _session(session_id).to_dict(),
            "runs": [item.to_dict() for item in runs.list(session_id=session_id)],
        }

    @app.post("/api/v1/research/sessions/{session_id}/messages", status_code=202)
    async def send_message(
        request: Request, session_id: str, body: MessageCreate
    ) -> dict[str, object]:
        deps.require_write(request)
        session = _session(session_id)
        if deps.model_turn_factory is None:
            raise _error(503, "The Research Lab has no configured model route.")
        config = deps.settings.load()
        overrides: dict[str, Mapping[str, object]] = {
            key: value
            for key, value in session.model_overrides.items()
            if isinstance(key, str) and isinstance(value, Mapping)
        }
        try:
            routes = resolve_research_lab_models(config, overrides)
        except SettingsError as exc:
            raise _error(422, str(exc)) from exc
        run = runs.create(
            session_id=session_id,
            prompt=body.prompt,
            model_config={
                agent_id: {
                    "tier": route.tier.value,
                    "provider": None
                    if route.provider is None
                    else route.provider.value,
                    "model": route.model,
                }
                for agent_id, route in routes.items()
            },
            params={},
        )
        run_root = runner.run_dir(run.run_id)
        runs.update_status(run.run_id, status="running")
        budgets = config.research_lab.budgets
        harness = ResearchHarness(
            ledger=ledger,
            tools=(_backtest_tool(run.run_id, run_root), _alpha_tool(run.run_id)),
            max_iterations=budgets.max_iterations,
            max_recovery_retries=budgets.max_recovery_retries,
            max_revision_retries=budgets.max_revision_retries,
        )
        turn = deps.model_turn_factory()

        async def work(emit: Emit) -> Mapping[str, object]:
            try:
                outcome: HarnessOutcome = await harness.run(
                    prompt=body.prompt, run_id=run.run_id, emit=emit, model_turn=turn
                )
            except (HarnessError, EvidenceError) as exc:
                runs.update_status(
                    run.run_id,
                    status="failed",
                    validation={"passed": False, "reason": str(exc)},
                    completed=True,
                )
                raise
            status = "completed" if outcome.status == "completed" else "blocked"
            runs.update_status(
                run.run_id,
                status=status,
                metrics={
                    "iterations": outcome.iterations,
                    "tools_used": list(outcome.tools_used),
                    "text": outcome.text,
                },
                validation=dict(outcome.validation),
                completed=True,
            )
            emit(
                "usage",
                {"model": None, "input_tokens": None, "output_tokens": None},
            )
            return {"run_id": run.run_id, "status": status, "text": outcome.text}

        try:
            await runner.start(session_id=session_id, run_id=run.run_id, work=work)
        except ResearchBusy as exc:
            raise _error(409, str(exc)) from exc
        return {"run_id": run.run_id, "status": "running"}

    @app.get("/api/v1/research/sessions/{session_id}/events")
    async def stream_events(
        request: Request, session_id: str, run_id: str | None = None, after: int = 0
    ) -> StreamingResponse:
        deps.read_auth(request)
        _session(session_id)
        target = run_id or runner.running_run(session_id)
        if target is None:
            raise _error(404, "No run is associated with this session yet.")
        _run(target)
        return StreamingResponse(
            _journal_stream(runner, target, max(0, after)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/research/sessions/{session_id}/cancel", status_code=202)
    async def cancel_session(request: Request, session_id: str) -> dict[str, object]:
        deps.require_write(request)
        _session(session_id)
        target = runner.running_run(session_id)
        if not await runner.cancel(session_id):
            raise _error(409, "That session has no executing run.")
        if target is not None:
            runs.update_status(
                target,
                status="cancelled",
                validation={"passed": False, "reason": "cancelled"},
                completed=True,
            )
        return {"status": "cancelled", "run_id": target}

    # ---- runs ------------------------------------------------------------

    @app.get("/api/v1/research/runs")
    def list_runs(
        request: Request, session_id: str | None = None, status: str | None = None
    ) -> dict[str, object]:
        deps.read_auth(request)
        try:
            records = runs.list(session_id=session_id, status=status)
        except ResearchLabError as exc:
            raise _error(422, str(exc)) from exc
        return {"runs": [item.to_dict() for item in records]}

    @app.get("/api/v1/research/runs/{run_id}")
    def get_run(request: Request, run_id: str) -> dict[str, object]:
        deps.read_auth(request)
        record = _run(run_id)
        payload = record.to_dict()
        root = _root(record)
        for name in ("metrics.json", "risk_xray.json", "equity.json"):
            path = root / name
            if path.is_file():
                payload[name] = json.loads(path.read_text(encoding="utf-8"))
        payload["evidence"] = [item.to_dict() for item in ledger.entries(run_id)]
        return payload

    @app.post("/api/v1/research/runs/{run_id}/replay", status_code=201)
    def replay_run(
        request: Request, run_id: str, body: ReplayRequest
    ) -> dict[str, object]:
        deps.require_write(request)
        parent = _run(run_id)
        if not parent.frozen_data_path or not parent.data_manifest_sha256:
            raise _error(422, "That run has no frozen inputs to replay.")
        raw_strategy = parent.params.get("strategy")
        if not isinstance(raw_strategy, Mapping):
            raise _error(422, "That run did not record a strategy definition.")
        try:
            definition = strategy_from_dict(raw_strategy)
            walk = (
                WalkForwardSpec(folds=body.folds, holdout_bars=body.holdout_bars or 0)
                if body.folds is not None
                else None
            )
            result = replay_from_frozen(
                parent.frozen_data_path,
                parent.data_manifest_sha256,
                definition,
                start=parse_timestamp(str(parent.params["start"])),
                end=parse_timestamp(str(parent.params["end"])),
                initial_capital_paise=1_000_000,
                walk_forward=walk,
            )
        except (
            FrozenManifestError,
            ReplayError,
            StrategyError,
            KeyError,
            ValueError,
        ) as exc:
            raise _error(422, str(exc)) from exc
        child = runs.create(
            session_id=parent.session_id,
            parent_run_id=parent.run_id,
            prompt=f"replay of {parent.run_id}",
            model_config={},
            params={"replay_of": parent.run_id, "folds": body.folds},
            status="running",
            data_manifest_sha256=result.manifest_sha256,
            frozen_data_path=str(parent.frozen_data_path),
        )
        root = runner.run_dir(child.run_id)
        (root / "trades.json").write_text(
            json.dumps([trade.to_dict() for trade in result.trades], indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "folds.json").write_text(
            json.dumps(
                {"folds": list(result.folds), "holdout": result.holdout}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        return runs.update_status(
            child.run_id,
            status="completed",
            metrics=result.metrics.to_dict(),
            completed=True,
        ).to_dict()

    @app.get("/api/v1/research/runs/{run_id}/export.csv")
    def export_run(
        request: Request, run_id: str, kind: str = "trades"
    ) -> PlainTextResponse:
        deps.read_auth(request)
        record = _run(run_id)
        root = _root(record)
        if kind == "trades":
            path = root / "trades.json"
            if not path.is_file():
                raise _error(404, "That run has no trades.")
            rows = json.loads(path.read_text(encoding="utf-8"))
            lines = ["symbol,entry_at,exit_at,net_pnl_paise,exit_reason"]
            for item in rows:
                lines.append(
                    ",".join(
                        [
                            str(item.get("symbol", "")),
                            str(item.get("entry_at", "")),
                            str(item.get("exit_at", "")),
                            str(item.get("net_pnl_paise", "")),
                            str(item.get("exit_reason", "")),
                        ]
                    )
                )
            body = "\n".join(lines) + "\n"
        elif kind == "folds":
            path = root / "folds.json"
            if not path.is_file():
                raise _error(404, "That run has no walk-forward folds.")
            payload = json.loads(path.read_text(encoding="utf-8"))
            body = folds_csv(payload.get("folds", []), payload.get("holdout"))
        else:
            raise _error(422, "kind must be trades or folds.")
        return PlainTextResponse(
            body,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{run_id}-{kind}.csv"'
            },
        )

    # ---- alpha bench and reconciliation ---------------------------------

    @app.post("/api/v1/research/alpha/bench")
    def bench(request: Request, body: AlphaBenchRequest) -> dict[str, object]:
        deps.require_write(request)
        try:
            return alpha_bench(
                _frame([dict(item) for item in body.bars]),
                _frame([dict(item) for item in body.factor]),
                horizons=tuple(body.horizons),
                source=body.source,
                membership_is_current=body.membership_is_current,
                missing_symbols=body.missing_symbols,
            )
        except (AlphaBenchError, ValueError) as exc:
            raise _error(422, str(exc)) from exc

    @app.post("/api/v1/research/paper/reconcile")
    def reconcile(request: Request, body: ReconcileRequest) -> dict[str, object]:
        deps.require_write(request)
        try:
            return paper_trade_reconcile(
                body.initial_cash_paise,
                body.fills,
                body.ending_prices,
                body.reported_state,
                tolerance_paise=body.tolerance_paise,
            )
        except ReconcileError as exc:
            raise _error(422, str(exc)) from exc

    # ---- artifacts -------------------------------------------------------

    @app.get("/api/v1/research/artifacts")
    def search_artifacts(request: Request, q: str | None = None) -> dict[str, object]:
        deps.read_auth(request)
        records: tuple[ResearchArtifactRecord, ...] = ()
        if q:
            try:
                records = artifacts.search(q)
            except ResearchLabError as exc:
                raise _error(422, str(exc)) from exc
        return {
            "artifacts": [item.to_dict() for item in records],
            "fts_available": artifacts.fts_available,
        }

    @app.post("/api/v1/research/artifacts", status_code=201)
    def save_artifact(request: Request, body: ArtifactCreate) -> dict[str, object]:
        deps.require_write(request)
        try:
            return artifacts.save(
                slug=body.slug,
                title=body.title,
                body_md=body.body_md,
                linked_run_ids=body.linked_run_ids,
                evidence_refs=body.evidence_refs,
            ).to_dict()
        except ResearchLabError as exc:
            raise _error(422, str(exc)) from exc

    @app.get("/api/v1/research/artifacts/{artifact_id}/diff")
    def diff_artifact(
        request: Request, artifact_id: str, rev_a: int, rev_b: int
    ) -> dict[str, object]:
        deps.read_auth(request)
        try:
            record = artifacts.get(artifact_id)
            return artifacts.diff(record.slug, rev_a, rev_b)
        except ResearchLabError as exc:
            raise _error(404, str(exc)) from exc

    @app.get("/api/v1/research/artifacts/{artifact_id}/export")
    def export_artifact(
        request: Request, artifact_id: str, format: str = "json"
    ) -> PlainTextResponse:
        deps.read_auth(request)
        try:
            record = artifacts.get(artifact_id)
        except ResearchLabError as exc:
            raise _error(404, str(exc)) from exc
        if format == "md":
            body, media = f"# {record.title}\n\n{record.body_md}\n", "text/markdown"
        elif format == "json":
            body, media = (
                json.dumps(record.to_dict(), indent=2) + "\n",
                "application/json",
            )
        else:
            raise _error(422, "format must be json or md.")
        return PlainTextResponse(
            body,
            media_type=media,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{record.slug}-r{record.revision}.{format}"'
                )
            },
        )

    # ---- model configuration --------------------------------------------

    def _models_payload() -> dict[str, object]:
        config = deps.settings.load()
        return {
            "research_lab": research_lab_payload(config),
            "routes": {
                agent_id: {
                    "tier": route.tier.value,
                    "provider": None
                    if route.provider is None
                    else route.provider.value,
                    "model": route.model,
                }
                for agent_id, route in resolve_research_lab_models(config).items()
            },
        }

    @app.get("/api/v1/research/models")
    def get_models(request: Request) -> dict[str, object]:
        deps.read_auth(request)
        return _models_payload()

    @app.put("/api/v1/research/models")
    def put_models(request: Request, body: ModelsUpdate) -> dict[str, object]:
        deps.require_write(request)
        try:
            deps.settings.save_research_lab(parse_research_lab(body.research_lab))
        except SettingsError as exc:
            raise _error(422, str(exc)) from exc
        return _models_payload()


async def _journal_stream(
    runner: ResearchRunner, run_id: str, after: int
) -> AsyncIterator[bytes]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SSE_MAX_SECONDS
    cursor = after
    last_sent = loop.time()
    while True:
        for event in runner.read_events(run_id, after=cursor):
            cursor = int(str(event["seq"]))
            yield _sse(event)
            last_sent = loop.time()
            if event.get("done"):
                return
        now = loop.time()
        if now >= deadline:
            yield _sse({"type": "error", "done": True, "detail": "stream timeout"})
            return
        if now - last_sent >= SSE_HEARTBEAT_SECONDS:
            yield b": keep-alive\n\n"
            last_sent = now
        await asyncio.sleep(SSE_POLL_SECONDS)


__all__ = [
    "ResearchDeps",
    "SSE_HEARTBEAT_SECONDS",
    "SSE_POLL_SECONDS",
    "register",
]
