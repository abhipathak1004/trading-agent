"""Run one fund operation in-process and stream honest progress.

This module used to own a subprocess: argv, a scoped environment, a process
group, and ``killpg`` to stop it. The engine is vendored now (ADR-053), so what
remains is the part that was never about the process — a bounded progress journal
the UI polls and the SSE route replays, and a runner that holds at most one
backtest at a time.

The journal keeps its exact keys (``run_id``, ``phase``, ``detail``,
``elapsed_seconds``, ``lines``, ``error``, ``done``) because they are a wire
contract: the panel polls ``progress.json`` and ``test_sse_stream_*`` asserts on
the frames. What changed is only who writes the phases.

What it will not do: pretend to report progress it does not have. A backtest is a
loop over sessions inside the engine; this reports phases and an elapsed clock,
and — because an in-process run *can* see each session — a completed-session
count, which a subprocess could never know.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bq.aihf.config import AiHfPaths, validate_name

MAX_LOG_LINES = 200
MAX_LOG_LINE_CHARS = 500

# Phase names are a wire contract (`hedgeFundContract.ts` PHASES, `phaseTone`).
PHASES = ("queued", "preparing", "running", "parsed", "failed", "cancelled")


class AiHfExecutionError(RuntimeError):
    """The operation could not be run at all (bad paths, nothing configured)."""


@dataclass(slots=True)
class Progress:
    """What a run is doing, written to ``runs/<id>/progress.json``.

    The UI polls this and the SSE endpoint replays it; neither needs to hold a
    socket open to know the run finished.
    """

    run_id: str
    phase: str = "queued"
    detail: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    finished_at: float | None = None
    lines: list[str] = field(default_factory=list)
    error: str | None = None
    # Only an in-process run can be this precise; a subprocess could not. Kept
    # optional so a reader that ignores these keys still works.
    sessions_done: int | None = None
    sessions_total: int | None = None

    def snapshot(self) -> dict[str, object]:
        elapsed = (
            (self.finished_at or time.time()) - self.started_at
            if self.started_at
            else 0.0
        )
        payload: dict[str, object] = {
            "run_id": self.run_id,
            "phase": self.phase,
            "detail": self.detail,
            "elapsed_seconds": round(max(0.0, elapsed), 1),
            "lines": list(self.lines),
            "error": self.error,
            "done": self.phase in ("parsed", "failed", "cancelled"),
        }
        if self.sessions_total is not None:
            payload["sessions_done"] = self.sessions_done or 0
            payload["sessions_total"] = self.sessions_total
        return payload

    def note(self, line: str) -> None:
        """Record one bounded progress line."""
        if len(self.lines) >= MAX_LOG_LINES:
            return
        self.lines.append(line[:MAX_LOG_LINE_CHARS])


def write_progress(path: Path, progress: Progress) -> None:
    progress.updated_at = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(progress.snapshot(), sort_keys=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def read_progress(path: Path, run_id: str) -> dict[str, object] | None:  # noqa: ARG001
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# one-at-a-time runner
# ---------------------------------------------------------------------------

# What a run's body is handed: the progress object it may annotate, and a check
# it can poll to see whether it has been cancelled. Both keep the body free of
# any knowledge of asyncio, threads or the journal file.
RunBody = Callable[[Progress, Callable[[], bool]], Any]


class AiHfRunner:
    """A single long-running fund operation at a time, cancellable.

    One instance per operation class (backtest, tick), so a preview can run while
    a backtest is still going, but two backtests cannot. The body runs in a worker
    thread (the engine is synchronous and CPU-bound), so cancellation is
    *cooperative*: there is no process to signal, and the body polls and returns.
    """

    def __init__(self, paths: AiHfPaths, *, name: str = "backtest") -> None:
        self.paths = paths
        self.name = name
        self._task: asyncio.Task[Any] | None = None
        self._run_id: str | None = None
        self._cancelled = False

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(
        self,
        run_id: str,
        body: RunBody,
        *,
        on_finish: Callable[[Any], Awaitable[None] | None] | None = None,
    ) -> None:
        if self.is_running():
            raise AiHfExecutionError(f"An {self.name} run is already executing.")
        safe = validate_name(run_id, what=f"{self.name} run")
        self._run_id = safe
        self._cancelled = False
        self._task = asyncio.create_task(self._execute(safe, body, on_finish))

    async def _execute(
        self,
        run_id: str,
        body: RunBody,
        on_finish: Callable[[Any], Awaitable[None] | None] | None,
    ) -> None:
        progress_path = self.paths.runs / run_id / "progress.json"
        progress = Progress(run_id=run_id, started_at=time.time())
        result: Any = None
        try:
            progress.phase = "preparing"
            write_progress(progress_path, progress)
            progress.phase = "running"
            write_progress(progress_path, progress)

            result = await asyncio.to_thread(body, progress, lambda: self._cancelled)

            progress.phase = "cancelled" if self._cancelled else "parsed"
        except asyncio.CancelledError:
            progress.phase = "cancelled"
            progress.finished_at = time.time()
            write_progress(progress_path, progress)
            raise
        except Exception as exc:  # noqa: BLE001 - recorded for the UI, then re-raised
            progress.phase = "failed"
            # `_failure_detail` keeps a traceback and a filesystem path off screen.
            progress.error = _failure_detail(exc)
            raise
        finally:
            progress.finished_at = time.time()
            write_progress(progress_path, progress)
            if on_finish is not None and result is not None and not self._cancelled:
                outcome = on_finish(result)
                if asyncio.iscoroutine(outcome):
                    await outcome
            self._run_id = None
            self._cancelled = False

    async def cancel(self, run_id: str) -> bool:
        """Ask the run to stop. Cooperative: the body polls and returns."""
        if self._run_id != run_id or self._task is None:
            return False
        self._cancelled = True
        try:
            await self._task
        except Exception:  # noqa: BLE001 - a cancel must never raise to the caller
            pass
        return True

    async def stop(self) -> None:
        """Called from the FastAPI lifespan; never leaves a task behind."""
        if self._task is not None and not self._task.done():
            self._cancelled = True
            try:
                await self._task
            except Exception:  # noqa: BLE001
                pass


def _failure_detail(exc: BaseException) -> str:
    """A bounded reason a user can act on.

    The engine's exceptions can name a filesystem path (a ledger file, a fund
    directory) and a traceback is never useful on screen, so this is the
    exception's own text, capped. A bare exception with no message still reads as
    its type.
    """
    text = str(exc).strip()
    if not text:
        text = type(exc).__name__
    return text[:400]


# ---------------------------------------------------------------------------
# filesystem helpers
# ---------------------------------------------------------------------------


def fund_names(paths: AiHfPaths) -> list[str]:
    """Every deployed fund directory, in name order."""
    if not paths.paper.is_dir():
        return []
    names: list[str] = []
    for entry in sorted(paths.paper.iterdir()):
        if (entry / "fund.yaml").is_file():
            try:
                names.append(validate_name(entry.name, what="paper fund"))
            except ValueError:
                continue
    return names


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


__all__ = [
    "AiHfExecutionError",
    "AiHfRunner",
    "MAX_LOG_LINE_CHARS",
    "MAX_LOG_LINES",
    "PHASES",
    "Progress",
    "RunBody",
    "fund_names",
    "read_json",
    "read_progress",
    "write_progress",
]
