"""Run one research job in-process and stream honest progress.

The stream is a *journal*, not a live queue: every event is appended to
``<run_dir>/events.jsonl`` with a monotonic ``seq``. The SSE route replays from
``after=<seq>``, so a reconnecting browser sees each event exactly once — the
same property ``aihf``'s ``progress.json`` stream gives, with a cursor instead of
a whole-file comparison.

Only one run per session may execute at a time. A second request is refused
rather than queued, because two concurrent runs on one session would interleave
their journals and make the cursor meaningless.

Cancellation is cooperative: the task is asked to stop, the work coroutine
observes it between steps, and the runner always writes a terminal event — so a
client is never left waiting on a stream that will not end.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from bq.research_lab.security import safe_resolve_path

JOURNAL_NAME: Final = "events.jsonl"
MAX_EVENTS: Final = 5000
MAX_EVENT_CHARS: Final = 20_000

EVENT_TYPES: Final = frozenset(
    {
        "step",
        "tool_call",
        "provenance",
        "evidence_verdict",
        "usage",
        "final",
        "cancelled",
        "error",
    }
)

Emit = Callable[[str, Mapping[str, object]], None]


class ResearchRunnerError(RuntimeError):
    """The runner cannot accept this job."""


class ResearchBusy(ResearchRunnerError):
    """A run is already executing for this session."""


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ResearchRunHandle:
    session_id: str
    run_id: str


class ResearchRunner:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._current: dict[str, str] = {}

    # ---- paths -----------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        directory = safe_resolve_path(f"runs/{run_id}", self.root)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def journal_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / JOURNAL_NAME

    # ---- journal ---------------------------------------------------------

    def append(self, run_id: str, event: Mapping[str, object]) -> dict[str, object]:
        """Append one event and return it with its assigned ``seq``."""
        kind = str(event.get("type", ""))
        if kind not in EVENT_TYPES:
            raise ResearchRunnerError(f"Unknown research event type: {kind}.")
        path = self.journal_path(run_id)
        current = self.read_events(run_id)
        if len(current) >= MAX_EVENTS:
            raise ResearchRunnerError("The run journal is full.")
        seq = int(str(current[-1]["seq"])) + 1 if current else 1
        payload: dict[str, object] = {"seq": seq, "type": kind, "at": _stamp()}
        for key, value in event.items():
            if key in {"seq", "type", "at"}:
                continue
            payload[key] = value
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        if len(encoded) > MAX_EVENT_CHARS:
            raise ResearchRunnerError("A research event is too large.")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
        return payload

    def read_events(self, run_id: str, *, after: int = 0) -> list[dict[str, object]]:
        path = self.journal_path(run_id)
        if not path.is_file():
            return []
        events: list[dict[str, object]] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if int(str(payload.get("seq", 0))) > after:
                    events.append(payload)
        return events

    def emitter(self, run_id: str) -> Emit:
        def _emit(event_type: str, payload: Mapping[str, object]) -> None:
            self.append(run_id, {"type": event_type, **payload})

        return _emit

    # ---- execution -------------------------------------------------------

    def is_running(self, session_id: str) -> bool:
        return session_id in self._tasks and not self._tasks[session_id].done()

    def running_run(self, session_id: str) -> str | None:
        return self._current.get(session_id)

    async def start(
        self,
        *,
        session_id: str,
        run_id: str,
        work: Callable[[Emit], Awaitable[Mapping[str, object] | None]],
    ) -> ResearchRunHandle:
        if self.is_running(session_id):
            raise ResearchBusy("A research run is already executing for this session.")
        emit = self.emitter(run_id)
        emit("step", {"name": "queued", "detail": "run accepted"})

        async def _execute() -> None:
            try:
                outcome = await work(emit)
            except asyncio.CancelledError:
                self.append(
                    run_id,
                    {"type": "cancelled", "done": True, "detail": "cancelled by owner"},
                )
                raise
            except Exception as exc:  # noqa: BLE001 - every failure must end the stream
                self.append(
                    run_id,
                    {"type": "error", "done": True, "detail": str(exc)[:500]},
                )
                return
            else:
                terminal = {"type": "final", "done": True}
                if outcome:
                    terminal.update(outcome)
                self.append(run_id, terminal)
            finally:
                self._current.pop(session_id, None)

        self._current[session_id] = run_id
        self._tasks[session_id] = asyncio.create_task(_execute())
        return ResearchRunHandle(session_id=session_id, run_id=run_id)

    def _has_terminal(self, run_id: str) -> bool:
        events = self.read_events(run_id)
        return bool(events) and bool(events[-1].get("done"))

    async def cancel(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        if task is None or task.done():
            return False
        run_id = self._current.get(session_id)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._tasks.pop(session_id, None)
        self._current.pop(session_id, None)
        # A task cancelled before its first turn never runs its handler, so the
        # terminal event is written here rather than trusting the coroutine.
        if run_id is not None and not self._has_terminal(run_id):
            self.append(
                run_id,
                {"type": "cancelled", "done": True, "detail": "cancelled by owner"},
            )
        return True

    async def stop(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self._current.clear()

    async def wait(self, session_id: str) -> None:
        task = self._tasks.get(session_id)
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def journal_available(root: Path | str, run_id: str) -> bool:
    try:
        path = safe_resolve_path(f"runs/{run_id}/{JOURNAL_NAME}", Path(root))
    except ValueError:
        return False
    return path.is_file()


__all__ = [
    "EVENT_TYPES",
    "Emit",
    "JOURNAL_NAME",
    "MAX_EVENTS",
    "ResearchBusy",
    "ResearchRunHandle",
    "ResearchRunner",
    "ResearchRunnerError",
    "journal_available",
]
