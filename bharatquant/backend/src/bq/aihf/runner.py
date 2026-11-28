"""Run ``aihf`` as a subprocess and stream honest progress.

``aihf`` is a CLI with no HTTP API, so this module is the whole integration
surface: it owns the argv, the scoped environment, the process group, and the
cancellation contract. It is modelled on ``bq.evals.runner.EvalRunner`` and
``bq.local_models.ModelPullRunner`` — same ``create_subprocess_exec`` shape,
same ``start_new_session`` + ``killpg`` escalation, same "never run inside an
HTTP handler".

What it will not do: pretend to report progress it does not have. ``aihf
backtest`` prints nothing per session until its final JSON, so the journal
carries phases (``preparing``/``running``/``parsed``/``failed``) and an elapsed
clock — not an invented percentage.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bq.aihf.config import AiHfConfigError, AiHfPaths, validate_name

TERMINATION_GRACE_SECONDS = 15.0
MAX_LOG_LINES = 200
MAX_LOG_LINE_CHARS = 500
POLL_SECONDS = 0.5

# aihf's own exit codes (hedge_fund/run.py): argparse rejects at 2, a FundHalted
# or NothingDue or LedgerError exits 1.
EXIT_ARGUMENT_ERROR = 2


class AiHfExecutionError(RuntimeError):
    """``aihf`` could not be run at all (missing sidecar, bad paths)."""


@dataclass(frozen=True, slots=True)
class AiHfResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def failure_detail(self) -> str:
        """A bounded reason a user can act on.

        aihf writes its status to stderr and an unhandled engine exception to
        stdout with a full traceback, which names filesystem paths and the
        aihf home. Neither belongs on screen, so a traceback is reduced to its
        final exception line.
        """
        if self.timed_out:
            return "The aihf run exceeded its time limit and was stopped."
        lines = [
            line.strip()
            for line in (self.stderr or self.stdout).splitlines()
            if line.strip()
        ]
        if not lines:
            return f"aihf exited with code {self.returncode}."
        for line in reversed(lines):
            if line.startswith(('File "', "Traceback", "During handling")):
                continue
            if "site-packages" in line or line.startswith("/"):
                continue
            return line[:400]
        return lines[-1][:400]


async def run_aihf(
    paths: AiHfPaths,
    args: Sequence[str],
    env: dict[str, str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    on_line: Callable[[str], None] | None = None,
) -> AiHfResult:
    """Run one ``aihf`` command to completion.

    The environment is the caller's ``get_aihf_runtime_env`` result; it is
    passed whole, so the child never inherits a stray key from the server.
    """
    if not paths.available:
        raise AiHfExecutionError(
            f"The aihf sidecar is not installed at {paths.venv}; run `just aihf-install`."
        )
    argv = [str(paths.executable), *args]
    started = time.monotonic()
    process: asyncio.subprocess.Process | None = None
    timed_out = False
    working_dir = cwd or paths.root
    working_dir.mkdir(parents=True, exist_ok=True)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(working_dir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            timed_out = True
            await _terminate(process)
            stdout, stderr = await process.communicate()
    except FileNotFoundError as exc:
        raise AiHfExecutionError(f"Could not start aihf: {exc}") from exc
    duration_ms = int((time.monotonic() - started) * 1000)
    out = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")
    if on_line is not None:
        for line in (out + err).splitlines()[-MAX_LOG_LINES:]:
            on_line(line[:MAX_LOG_LINE_CHARS])
    return AiHfResult(
        argv=tuple(argv),
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=out,
        stderr=err,
        duration_ms=duration_ms,
        timed_out=timed_out,
    )


def run_aihf_sync(
    paths: AiHfPaths, args: Sequence[str], *, timeout: float = 120.0
) -> AiHfResult:
    """A quick, synchronous ``aihf`` call for short commands (seeding, listing).

    Only for sub-second work in a request path; anything long belongs in
    ``run_aihf`` on the event loop.
    """
    if not paths.available:
        raise AiHfExecutionError(
            f"The aihf sidecar is not installed at {paths.venv}; run `just aihf-install`."
        )
    from bq.aihf.runtime_env import _clean_base

    argv = [str(paths.executable), *args]
    # subprocess fails with FileNotFoundError for a missing cwd, not only a
    # missing argv[0], which reads as a sidecar problem rather than a path one.
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.home.mkdir(parents=True, exist_ok=True)
    base = _clean_base(None)
    base["HOME"] = str(paths.home)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(paths.root),
            env=base,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AiHfExecutionError("The aihf command did not finish in time.") from exc
    except OSError as exc:
        raise AiHfExecutionError(f"Could not start aihf: {exc}") from exc
    return AiHfResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout.decode("utf-8", "replace"),
        stderr=completed.stderr.decode("utf-8", "replace"),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=TERMINATION_GRACE_SECONDS)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ---------------------------------------------------------------------------
# progress journal
# ---------------------------------------------------------------------------

PHASES = ("queued", "preparing", "running", "parsed", "failed", "cancelled")


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

    def snapshot(self) -> dict[str, object]:
        elapsed = (
            (self.finished_at or time.time()) - self.started_at
            if self.started_at
            else 0.0
        )
        return {
            "run_id": self.run_id,
            "phase": self.phase,
            "detail": self.detail,
            "elapsed_seconds": round(max(0.0, elapsed), 1),
            "lines": list(self.lines),
            "error": self.error,
            "done": self.phase in ("parsed", "failed", "cancelled"),
        }


def write_progress(path: Path, progress: Progress) -> None:
    progress.updated_at = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(progress.snapshot(), sort_keys=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def read_progress(path: Path, run_id: str) -> dict[str, object] | None:
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


class AiHfRunner:
    """A single long-running ``aihf`` invocation at a time, cancellable.

    One instance per operation class (backtest, tick), so a preview can run
    while a backtest is still going, but two backtests cannot.
    """

    def __init__(
        self,
        paths: AiHfPaths,
        env_factory: Callable[[str | None], dict[str, str]],
        *,
        name: str = "backtest",
    ) -> None:
        self.paths = paths
        self.env_factory = env_factory
        self.name = name
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._run_id: str | None = None
        self._progress = Progress(run_id="")

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(
        self,
        run_id: str,
        args: Sequence[str],
        *,
        model: str | None = None,
        timeout: float | None = None,
        on_finish: Callable[[AiHfResult], Awaitable[None] | None] | None = None,
    ) -> None:
        if self.is_running():
            raise AiHfExecutionError(f"An aihf {self.name} run is already executing.")
        self._run_id = run_id
        self._progress = Progress(run_id=run_id, started_at=time.time())
        self._task = asyncio.create_task(
            self._execute(args, model=model, timeout=timeout, on_finish=on_finish)
        )

    async def _execute(
        self,
        args: Sequence[str],
        *,
        model: str | None,
        timeout: float | None,
        on_finish: Callable[[AiHfResult], Awaitable[None] | None] | None,
    ) -> None:
        progress_path = self.paths.runs / (self._run_id or "unknown") / "progress.json"
        result: AiHfResult | None = None
        try:
            self._progress.phase = "preparing"
            write_progress(progress_path, self._progress)

            env = self.env_factory(model)
            self._progress.phase = "running"
            self._progress.detail = " ".join(args[:2])
            write_progress(progress_path, self._progress)

            self._process = await asyncio.create_subprocess_exec(
                *[str(self.paths.executable), *args],
                cwd=str(self.paths.root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            stream = self._process.stdout
            chunks: list[str] = []
            if stream is not None:
                async for raw in stream:
                    line = raw.decode("utf-8", "replace").rstrip()
                    chunks.append(line)
                    if len(self._progress.lines) < MAX_LOG_LINES:
                        self._progress.lines.append(line[:MAX_LOG_LINE_CHARS])
                        write_progress(progress_path, self._progress)
            await self._process.wait()
            returncode = (
                self._process.returncode if self._process.returncode is not None else -1
            )
            result = AiHfResult(
                argv=tuple(args),
                returncode=returncode,
                stdout="\n".join(chunks),
                stderr="",
                duration_ms=0,
            )
            if returncode == 0:
                self._progress.phase = "parsed"
            else:
                self._progress.phase = "failed"
                self._progress.error = result.failure_detail()
        except asyncio.CancelledError:
            self._progress.phase = "cancelled"
            self._progress.finished_at = time.time()
            write_progress(progress_path, self._progress)
            raise
        except Exception as exc:  # noqa: BLE001 - recorded for the UI, then re-raised
            self._progress.phase = "failed"
            self._progress.error = f"{type(exc).__name__}: {exc}"[:400]
            raise
        finally:
            self._progress.finished_at = time.time()
            write_progress(progress_path, self._progress)
            self._process = None
            if on_finish is not None and result is not None:
                outcome = on_finish(result)
                if asyncio.iscoroutine(outcome):
                    await outcome
            self._run_id = None

    async def cancel(self, run_id: str) -> bool:
        if self._run_id != run_id or self._task is None:
            return False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return True

    async def stop(self) -> None:
        """Called from the FastAPI lifespan; never leaves an orphan."""
        if self._process is not None:
            await _terminate(self._process)
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# filesystem helpers
# ---------------------------------------------------------------------------


def copy_tree(source: Path, destination: Path) -> None:
    """Copy a fund directory for a sandboxed preview."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)


def remove_tree(path: Path) -> None:
    """Remove a scratch directory. Never points at the live fund by construction."""
    shutil.rmtree(path, ignore_errors=True)


def directory_digest(path: Path) -> str:
    """A stable digest of a fund directory, so a preview can prove it changed nothing."""
    import hashlib

    digest = hashlib.sha256()
    if not path.is_dir():
        return ""
    for entry in sorted(path.rglob("*")):
        if entry.is_file():
            digest.update(entry.relative_to(path).as_posix().encode())
            digest.update(entry.read_bytes())
    return digest.hexdigest()


def fund_names(paths: AiHfPaths) -> list[str]:
    """Every deployed fund directory, newest name order."""
    if not paths.paper.is_dir():
        return []
    names: list[str] = []
    for entry in sorted(paths.paper.iterdir()):
        if (entry / "fund.yaml").is_file():
            try:
                names.append(validate_name(entry.name, what="paper fund"))
            except AiHfConfigError:
                continue
    return names


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
