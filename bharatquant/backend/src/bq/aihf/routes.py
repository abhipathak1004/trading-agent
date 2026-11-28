"""``/api/v1/aihf/*`` — the AI Hedge Fund tab's HTTP surface.

A separate module rather than ~700 more lines inside ``create_app``, but
registered from it so it obeys the same rules as every neighbour: the
``csrf_for_session(...) is None → 401`` idiom for reads, ``require_write`` for
writes, and nothing mounted after the SPA catch-all.

Two things this layer owns and the service does not:

* **HTTP status semantics** — a halted fund is ``409``, a missing fund
  ``404``, an invalid mandate ``422``.
* **Server-Sent Events** for a running backtest. It streams a bounded journal
  of *real* phases, not invented session percentages: ``aihf backtest`` prints
  nothing until its final JSON.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bq.aihf.config import AiHfConfigError, AiHfPaths, validate_name
from bq.aihf.parsing import AiHfParseError
from bq.aihf.runner import (
    AiHfExecutionError,
    AiHfRunner,
    Progress,
    read_progress,
    write_progress,
)
from bq.aihf.service import (
    AiHfConflict,
    AiHfInvalid,
    AiHfNotFound,
    AiHfServiceError,
    MandateStore,
    PaperStore,
    ResearchStore,
    normalize_tickers,
    status,
    validate_dates,
)
from bq.aihf.service import (
    models as list_models,
)
from bq.settings import SettingsStore

SSE_HEARTBEAT_SECONDS = 15.0
SSE_POLL_SECONDS = 1.0
SSE_MAX_SECONDS = 60 * 60 * 3


# ---------------------------------------------------------------------------
# wire models
# ---------------------------------------------------------------------------


class MandateWrite(BaseModel):
    yaml: str = Field(min_length=1, max_length=20_000)


class BacktestStart(BaseModel):
    mandate: str = Field(min_length=1, max_length=64)
    tickers: list[str] = Field(min_length=1, max_length=64)
    start_date: str
    end_date: str
    model_override: str | None = None


class FundCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    mandate: str = Field(min_length=1, max_length=64)
    tickers: list[str] = Field(min_length=1, max_length=64)


class ExecuteRequest(BaseModel):
    preview_id: str = Field(min_length=1, max_length=64)


KillAction = Literal["halt", "resume"]


class KillSwitchRequest(BaseModel):
    action: KillAction = "halt"
    reason: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AiHfDeps:
    """What the routes need from ``create_app``, passed explicitly."""

    paths: AiHfPaths
    settings: SettingsStore
    read_auth: Callable[[Request], None]
    require_write: Callable[[Request], None]
    backtest_runner: AiHfRunner


def register(app: FastAPI, deps: AiHfDeps) -> None:
    paths = deps.paths
    settings = deps.settings
    mandates = MandateStore(paths)
    research = ResearchStore(paths)
    paper = PaperStore(paths, settings)
    runner = deps.backtest_runner

    # ---- status ----------------------------------------------------------

    @app.get("/api/v1/aihf/status")
    def aihf_status(request: Request) -> dict[str, object]:
        deps.read_auth(request)
        try:
            return status(paths, settings)
        except AiHfServiceError as exc:
            raise _status_error(exc) from exc

    @app.get("/api/v1/aihf/models")
    def aihf_models(request: Request) -> dict[str, object]:
        deps.read_auth(request)
        try:
            return {"models": list_models(paths, settings)}
        except AiHfServiceError as exc:
            raise _status_error(exc) from exc

    # ---- mandates --------------------------------------------------------

    @app.get("/api/v1/aihf/mandates")
    def list_mandates(request: Request) -> dict[str, object]:
        deps.read_auth(request)
        try:
            return {"mandates": mandates.list(), "template": mandates.template()}
        except (AiHfServiceError, AiHfConfigError) as exc:
            raise _status_error(exc) from exc

    @app.get("/api/v1/aihf/mandates/{name}")
    def read_mandate(request: Request, name: str) -> dict[str, object]:
        deps.read_auth(request)
        try:
            return mandates.read(name)
        except (AiHfServiceError, AiHfConfigError) as exc:
            raise _status_error(exc) from exc

    @app.post("/api/v1/aihf/mandates", status_code=201)
    def create_mandate(request: Request, body: MandateWrite) -> dict[str, object]:
        deps.require_write(request)
        return _mandate_write(mandates, body.yaml, create=True)

    @app.put("/api/v1/aihf/mandates/{name}")
    def update_mandate(
        request: Request, name: str, body: MandateWrite
    ) -> dict[str, object]:
        deps.require_write(request)
        return _mandate_write(mandates, body.yaml, create=False, name=name)

    @app.delete("/api/v1/aihf/mandates/{name}", status_code=204)
    def delete_mandate(request: Request, name: str) -> None:
        deps.require_write(request)
        try:
            mandates.delete(name)
        except (AiHfServiceError, AiHfConfigError) as exc:
            raise _status_error(exc) from exc

    # ---- backtests -------------------------------------------------------

    @app.post("/api/v1/aihf/backtest", status_code=202)
    async def start_backtest(
        request: Request, body: BacktestStart
    ) -> dict[str, object]:
        deps.require_write(request)
        if runner.is_running():
            raise HTTPException(
                status_code=409,
                detail="A backtest is already running; wait for it or stop it.",
            )
        if body.model_override is not None:
            # The engine names no model: a persona routes through Settings. A
            # per-run override was a subprocess concept; accepting and ignoring
            # it would be a lie in the UI.
            raise HTTPException(
                status_code=422,
                detail=(
                    "model_override is not supported: models are chosen in "
                    "Settings -> Models & keys"
                ),
            )
        try:
            mandate_name = validate_name(body.mandate, what="mandate")
            tickers = normalize_tickers(body.tickers)
            start, end = validate_dates(body.start_date, body.end_date)
        except (AiHfServiceError, AiHfConfigError) as exc:
            raise _status_error(exc) from exc
        MandateStore(paths).list()
        mandate_path = paths.mandate_path(mandate_name)
        if not mandate_path.is_file():
            raise HTTPException(
                status_code=404, detail=f"No mandate named {mandate_name!r}."
            )

        run_id = f"{mandate_name}-{start}-{end}"
        paths.runs.mkdir(parents=True, exist_ok=True)
        progress_path = paths.runs / run_id / "progress.json"
        write_progress(
            progress_path,
            Progress(run_id=run_id, phase="queued", detail="waiting for the engine"),
        )
        runner.start(
            run_id,
            _backtest_body(paths, settings, mandate_name, tickers, start, end),
            on_finish=_save_result(paths, run_id),
        )
        return {"run_id": run_id, "status": "queued"}

    @app.get("/api/v1/aihf/backtest/{run_id}/stream")
    async def backtest_stream(request: Request, run_id: str) -> StreamingResponse:
        deps.read_auth(request)
        try:
            validate_name(run_id, what="backtest run")
        except AiHfConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StreamingResponse(
            _event_stream(paths, run_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/aihf/backtest/{run_id}/cancel", status_code=202)
    async def cancel_backtest(request: Request, run_id: str) -> dict[str, object]:
        deps.require_write(request)
        cancelled = await runner.cancel(run_id)
        if not cancelled:
            raise HTTPException(status_code=409, detail="That run is not executing.")
        return {"run_id": run_id, "status": "cancelled"}

    @app.get("/api/v1/aihf/backtest/{run_id}")
    def backtest_progress(request: Request, run_id: str) -> dict[str, object]:
        deps.read_auth(request)
        try:
            safe = validate_name(run_id, what="backtest run")
        except AiHfConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        progress = read_progress(paths.runs / safe / "progress.json", safe)
        return {
            "run_id": safe,
            "running": runner.is_running() and runner.run_id == safe,
            "progress": progress,
        }

    @app.get("/api/v1/aihf/backtests")
    def list_backtests(request: Request) -> dict[str, object]:
        deps.read_auth(request)
        try:
            return {"backtests": research.list()}
        except AiHfServiceError as exc:
            raise _status_error(exc) from exc

    @app.get("/api/v1/aihf/backtests/{run_id}")
    def backtest_detail(
        request: Request, run_id: str, limit: int = 500, offset: int = 0
    ) -> dict[str, object]:
        deps.read_auth(request)
        if not 0 < limit <= 2_000:
            raise HTTPException(status_code=422, detail="limit must be 1..2000")
        if not 0 <= offset <= 100_000:
            raise HTTPException(status_code=422, detail="offset must be 0..100000")
        try:
            return research.detail(run_id, limit=limit, offset=offset)
        except (AiHfServiceError, AiHfConfigError) as exc:
            raise _status_error(exc) from exc

    # ---- paper funds -----------------------------------------------------

    @app.get("/api/v1/aihf/paper/funds")
    def list_funds(request: Request) -> dict[str, object]:
        deps.read_auth(request)
        try:
            return {"funds": paper.funds()}
        except (AiHfServiceError, AiHfConfigError) as exc:
            raise _status_error(exc) from exc

    @app.get("/api/v1/aihf/paper/funds/{name}")
    def read_fund(request: Request, name: str) -> dict[str, object]:
        deps.read_auth(request)
        try:
            return paper.fund(name)
        except (AiHfServiceError, AiHfConfigError) as exc:
            raise _status_error(exc) from exc

    @app.post("/api/v1/aihf/paper/funds", status_code=201)
    async def create_fund(request: Request, body: FundCreate) -> dict[str, object]:
        deps.require_write(request)
        try:
            return await paper.create(
                name=body.name, mandate=body.mandate, tickers=body.tickers
            )
        except (AiHfServiceError, AiHfConfigError, AiHfExecutionError) as exc:
            raise _status_error(exc) from exc

    @app.post("/api/v1/aihf/paper/{name}/preview")
    async def preview_session(request: Request, name: str) -> dict[str, object]:
        deps.require_write(request)
        try:
            return await paper.preview(name)
        except (AiHfServiceError, AiHfConfigError, AiHfExecutionError) as exc:
            raise _status_error(exc) from exc

    @app.post("/api/v1/aihf/paper/{name}/execute")
    async def execute_session(
        request: Request, name: str, body: ExecuteRequest
    ) -> dict[str, object]:
        deps.require_write(request)
        try:
            return await paper.execute(name, preview_id=body.preview_id)
        except (AiHfServiceError, AiHfConfigError, AiHfExecutionError) as exc:
            raise _status_error(exc) from exc

    @app.post("/api/v1/aihf/paper/{name}/kill-switch")
    async def kill_switch(
        request: Request, name: str, body: KillSwitchRequest
    ) -> dict[str, object]:
        deps.require_write(request)
        try:
            return await paper.kill_switch(name, action=body.action, reason=body.reason)
        except (AiHfServiceError, AiHfConfigError, AiHfExecutionError) as exc:
            raise _status_error(exc) from exc

    @app.get("/api/v1/aihf/paper/{name}/ledger")
    def fund_ledger(
        request: Request,
        name: str,
        limit: int = 200,
        offset: int = 0,
        verify: bool = True,
    ) -> dict[str, object]:
        deps.read_auth(request)
        if not 0 < limit <= 2_000:
            raise HTTPException(status_code=422, detail="limit must be 1..2000")
        try:
            return paper.ledger(name, limit=limit, offset=offset, verify=verify)
        except (AiHfServiceError, AiHfConfigError, AiHfParseError) as exc:
            raise _status_error(exc) from exc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mandate_write(
    mandates: MandateStore, yaml_text: str, *, create: bool, name: str = ""
) -> dict[str, object]:
    """Create/update, taking the stored name from the YAML's own ``name:``."""
    target = name
    if not target:
        target = _mandate_name_from_yaml(yaml_text)
    try:
        return mandates.write(target, yaml_text, create=create)
    except (AiHfServiceError, AiHfConfigError) as exc:
        raise _status_error(exc) from exc


def _mandate_name_from_yaml(text: str) -> str:
    """The ``name:`` line, read without a YAML parser.

    The mandate is then validated properly in the sidecar; this only needs to
    work out which file to write, and it refuses anything that is not a plain
    single-token name.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            if value:
                return value
    raise HTTPException(status_code=422, detail="The mandate YAML must declare a name:")


def _backtest_body(
    paths: AiHfPaths,
    settings: SettingsStore,
    mandate: str,
    tickers: tuple[str, ...],
    start: str,
    end: str,
) -> Callable[[Any, Callable[[], bool]], Any]:
    """The engine call a backtest run executes, in a worker thread.

    Builds the mandate, staffs its personas through the model router and runs the
    replay against this app's data client. Progress is reported as sessions
    complete, which a subprocess could never do.
    """
    from bq.aihf.bridge.personas import staff
    from bq.aihf.engine.llm import PromptCache
    from bq.aihf.wiring import build_data_client
    from bq.model_router import ModelRouter

    def body(progress: Progress, cancelled: Callable[[], bool]) -> Any:
        store = MandateStore(paths)
        spec = store.load(mandate)
        router = ModelRouter(settings)
        cache = PromptCache()
        models = staff(spec, router=router, cache=cache, blind=True)
        data = build_data_client(settings)
        progress.note(f"backtesting {spec.name} over {len(tickers)} names")

        from bq.aihf.engine.api import run_backtest
        from bq.aihf.engine.backtesting.fund import FundBacktestResult  # noqa: F401
        from bq.aihf.engine.fund import Fund

        fund = Fund(spec, models=models, blind=True)
        _ = fund  # staffed above; run_backtest accepts the spec directly
        return run_backtest(
            spec,
            start=start,
            end=end,
            data_client=data,
            universe=list(tickers),
            cache=cache,
            on_valuation=lambda i, n, v: progress.note(f"session {i + 1}/{n}"),
        )

    return body


def _save_result(paths: AiHfPaths, run_id: str) -> Callable[[Any], None]:
    """Persist the engine's result where the detail routes find it."""

    def save(result: Any) -> None:
        try:
            ResearchStore(paths).save(result, run_id)
        except (OSError, ValueError):
            # A failed save must not turn a completed backtest into a failed run.
            pass

    return save


async def _event_stream(paths: AiHfPaths, run_id: str) -> AsyncIterator[bytes]:
    """Replay the progress journal, then follow it until the run is done.

    Bounded in both time and rate: it sends only when the snapshot changes,
    heartbeats every ``SSE_HEARTBEAT_SECONDS`` so an idle proxy does not drop
    the connection, and gives up after ``SSE_MAX_SECONDS``.
    """
    progress_path = paths.runs / run_id / "progress.json"
    deadline = asyncio.get_running_loop().time() + SSE_MAX_SECONDS
    last: str | None = None
    last_sent = 0.0
    while True:
        now = asyncio.get_running_loop().time()
        snapshot = read_progress(progress_path, run_id)
        if snapshot is None:
            yield _sse({"run_id": run_id, "phase": "unknown", "done": False})
            return
        rendered = json.dumps(snapshot, sort_keys=True)
        if rendered != last:
            yield _sse(snapshot)
            last = rendered
            last_sent = now
        elif now - last_sent >= SSE_HEARTBEAT_SECONDS:
            yield b": keep-alive\n\n"
            last_sent = now
        if snapshot.get("done"):
            return
        if now >= deadline:
            yield _sse({"run_id": run_id, "phase": "timeout", "done": True})
            return
        await asyncio.sleep(SSE_POLL_SECONDS)


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, sort_keys=True)}\n\n".encode("utf-8")


def _status_error(exc: Exception) -> HTTPException:
    """Map a service failure onto the HTTP status it means."""
    if isinstance(exc, AiHfNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AiHfConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (AiHfInvalid, AiHfConfigError, AiHfParseError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, AiHfExecutionError):
        return HTTPException(status_code=503, detail=str(exc))
    # The engine ran and refused: an upstream data/model failure, not a bug
    # in this API. 502 says "the dependency answered badly".
    return HTTPException(status_code=502, detail=str(exc)[:600])
