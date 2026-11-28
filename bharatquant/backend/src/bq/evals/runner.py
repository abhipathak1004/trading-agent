"""Sequential Promptfoo execution for the Model Evals tab.

One benchmark at a time is a hard constraint: each local LLM loads fully into
24 GB unified memory, so ``--max-concurrency 1`` is non-negotiable and the whole
sweep runs in a dedicated asyncio task, never inside an HTTP handler. Cancelling
signals the subprocess group with SIGTERM and escalates to SIGKILL.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bq.evals.config import MIN_NODE_VERSION, EvalsPaths, supported_node_dir
from bq.evals.scoring import analyze_results_file, export_scorecard_csv
from bq.evals.store import EvalRun, EvalStore

if TYPE_CHECKING:
    from collections.abc import Sequence

PROMPTFOO_PACKAGE = "promptfoo@latest"
TERMINATION_GRACE_SECONDS = 15.0
STDERR_TAIL_CHARS = 2000
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


class EvalRunError(RuntimeError):
    """A Promptfoo sweep could not be started or did not finish cleanly."""


@dataclass(frozen=True, slots=True)
class EvalLaunch:
    run: EvalRun
    providers: tuple[str, ...]
    no_cache: bool


def _actionable_error(stderr: bytes | None, returncode: int = 1) -> str:
    """Prefer the last meaningful line over npm's engine warnings."""
    text = (stderr or b"").decode("utf-8", "replace")
    cleaned = ANSI_ESCAPE.sub("", text)
    skip = (
        "npm warn",
        "npm notice",
        "\u001b",
        "^",
        "Detected:",
        "Required:",
        "current:",
    )
    lines = [
        line.strip()
        for line in cleaned.splitlines()
        if line.strip() and not line.strip().startswith(skip)
    ]
    if lines:
        return lines[-1][:STDERR_TAIL_CHARS]
    return f"promptfoo exited {returncode}"


def build_promptfoo_command(
    *,
    paths: EvalsPaths,
    artifact_dir: Path,
    providers: Sequence[str],
    categories: Sequence[str] = (),
    no_cache: bool,
    config_path: Path | None = None,
) -> list[str]:
    """Build the sequential ``promptfoo eval`` argv (never parallel)."""
    command = [
        "npx",
        PROMPTFOO_PACKAGE,
        "eval",
        "--max-concurrency",
        "1",
        "-c",
        str(config_path or paths.config),
        "-o",
        str(artifact_dir / "results.json"),
    ]
    if no_cache:
        command.append("--no-cache")
    if providers:
        # Promptfoo treats --filter-providers as one regex, not a comma list.
        alternatives = "|".join(re.escape(item) for item in providers)
        command.extend(["--filter-providers", f"^(?:{alternatives})$"])
    if categories:
        command.extend(["--filter-metadata", f"category={','.join(categories)}"])
    return command


class EvalRunner:
    """Owns the one running Promptfoo process and its asyncio task."""

    def __init__(self, store: EvalStore, paths: EvalsPaths) -> None:
        self.store = store
        self.paths = paths
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._run_id: str | None = None

    @property
    def active_run_id(self) -> str | None:
        return self._run_id

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, launch: EvalLaunch) -> None:
        if self.is_running():
            raise EvalRunError("An eval run is already executing.")
        self._run_id = launch.run.id
        self._task = asyncio.create_task(self._execute(launch))

    async def _execute(self, launch: EvalLaunch) -> None:
        run_id = launch.run.id
        artifact_dir = self.store.artifact_dir(run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        run = self.store.get_run(run_id)
        command = build_promptfoo_command(
            paths=self.paths,
            artifact_dir=artifact_dir,
            providers=launch.providers,
            categories=run.categories,
            no_cache=launch.no_cache,
            config_path=Path(run.config_path),
        )
        node_dir = supported_node_dir()
        if node_dir is None:
            minimum = ".".join(str(part) for part in MIN_NODE_VERSION)
            self._run_id = None
            self.store.mark_failed(
                run_id,
                f"No supported Node.js (>={minimum}) found for npx.",
            )
            return
        env = {
            **os.environ,
            "PROMPTFOO_DISABLE_TELEMETRY": "1",
            "PATH": node_dir + os.pathsep + os.environ.get("PATH", ""),
        }
        try:
            self.store.mark_started(run_id)
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.paths.root.parent,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            _, stderr = await self._process.communicate()
            returncode = self._process.returncode or 0
        except asyncio.CancelledError:
            await self._terminate()
            self.store.mark_cancelled(run_id)
            raise
        except Exception as exc:  # noqa: BLE001 - record and stop cleanly
            self.store.mark_failed(run_id, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._process = None
            self._run_id = None

        if returncode == -signal.SIGTERM or returncode == -signal.SIGKILL:
            self.store.mark_cancelled(run_id)
            return

        results_path = artifact_dir / "results.json"
        if not results_path.is_file():
            # No output file: Promptfoo itself failed (missing model, bad config,
            # rejected flags). A benchmark that merely has failing cases still
            # writes results.json and exits PROMPTFOO_FAILED_TEST_EXIT_CODE (100).
            self.store.mark_failed(run_id, _actionable_error(stderr, returncode))
            return

        try:
            summaries, categories = analyze_results_file(
                self.paths.scorer, results_path
            )
            if not summaries:
                self.store.mark_failed(
                    run_id,
                    "Promptfoo produced no case results; check the selected "
                    "models and filters.",
                )
                return
            self.store.save_scores(run_id, summaries)
            export_scorecard_csv(
                self.paths.scorer,
                summaries,
                categories,
                artifact_dir / "scorecard.csv",
            )
        except Exception as exc:  # noqa: BLE001 - the sweep ran, scoring failed
            self.store.mark_failed(run_id, f"scoring failed: {exc}")
            return
        self.store.mark_completed(run_id, str(results_path))

    async def _terminate(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATION_GRACE_SECONDS)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            await process.wait()

    async def cancel(self, run_id: str) -> bool:
        """Cancel ``run_id`` if it is the active sweep. Returns whether it was."""
        if self._run_id != run_id or self._task is None:
            return False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return True

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._run_id = None


__all__ = [
    "EvalLaunch",
    "EvalRunError",
    "EvalRunner",
    "build_promptfoo_command",
]
