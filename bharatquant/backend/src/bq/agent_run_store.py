"""Append-only history for the interactive research agent loop.

One row per run and one row per observable step. Bounds mirror the loop itself
(``bq.agent_runtime``) and the API request limits, so a stored record is always a
faithful, bounded copy of what the user saw.
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

from bq.core import Clock, SystemClock, open_connection, parse_timestamp, utc

IDENTIFIER = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._:-]{0,127}")
MAX_TASK_CHARS = 4_000
MAX_FINAL_TEXT_CHARS = 12_000
MAX_STEP_OUTPUT_CHARS = 12_000
MAX_ARGUMENTS_JSON = 8_192


class AgentRunError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AgentRunStep:
    turn: int
    kind: str
    tool: str
    arguments: dict[str, object]
    output: str

    def to_dict(self) -> dict[str, object]:
        return {
            "turn": self.turn,
            "kind": self.kind,
            "tool": self.tool,
            "arguments": self.arguments,
            "output": self.output,
        }


@dataclass(frozen=True, slots=True)
class AgentRun:
    run_id: str
    agent_id: str
    task: str
    final_text: str
    turns: int
    stopped: bool
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    started_at: datetime
    finished_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "task": self.task,
            "final_text": self.final_text,
            "turns": self.turns,
            "stopped": self.stopped,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
        }


def _timestamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


class AgentRunStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def record(
        self,
        *,
        agent_id: str,
        task: str,
        final_text: str,
        turns: int,
        stopped: bool,
        provider: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        steps: tuple[AgentRunStep, ...],
        started_at: datetime,
    ) -> AgentRun:
        if not isinstance(agent_id, str) or not 1 <= len(agent_id) <= 64:
            raise AgentRunError("Agent id is required.")
        if not isinstance(task, str) or not 1 <= len(task) <= MAX_TASK_CHARS:
            raise AgentRunError("Task is empty or too long.")
        if not isinstance(final_text, str) or len(final_text) > MAX_FINAL_TEXT_CHARS:
            raise AgentRunError("Final text is too long.")
        if (
            isinstance(turns, bool)
            or not isinstance(turns, int)
            or not 0 <= turns <= 64
        ):
            raise AgentRunError("Turns must be a nonnegative integer.")
        for label, value in (("input", input_tokens), ("output", output_tokens)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise AgentRunError(f"{label} tokens must be null or nonnegative.")

        run_id = secrets.token_urlsafe(18)
        finished_at = utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO agent_runs "
                    "(run_id, agent_id, task, final_text, turns, stopped, provider, "
                    "model, input_tokens, output_tokens, started_at, finished_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        agent_id,
                        task,
                        final_text,
                        turns,
                        1 if stopped else 0,
                        provider[:64],
                        model[:200],
                        input_tokens,
                        output_tokens,
                        _timestamp(started_at),
                        _timestamp(finished_at),
                    ),
                )
                for step in steps:
                    self._insert_step(connection, run_id, step)
        return self.get(run_id)

    @staticmethod
    def _insert_step(
        connection: sqlite3.Connection, run_id: str, step: AgentRunStep
    ) -> None:
        arguments_json = json.dumps(
            step.arguments, sort_keys=True, separators=(",", ":")
        )
        if len(arguments_json) > MAX_ARGUMENTS_JSON:
            raise AgentRunError("A step's arguments are too large.")
        if not isinstance(step.kind, str) or step.kind not in {
            "tool",
            "blocked",
            "error",
        }:
            raise AgentRunError("Unknown step kind.")
        if len(step.output) > MAX_STEP_OUTPUT_CHARS:
            raise AgentRunError("A step's output is too large.")
        connection.execute(
            "INSERT INTO agent_run_steps "
            "(run_id, turn, kind, tool, arguments_json, output) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, step.turn, step.kind, step.tool, arguments_json, step.output),
        )

    def get(self, run_id: str) -> AgentRun:
        if not isinstance(run_id, str) or IDENTIFIER.fullmatch(run_id) is None:
            raise AgentRunError("Invalid run identifier.")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT run_id, agent_id, task, final_text, turns, stopped, provider, "
                "model, input_tokens, output_tokens, started_at, finished_at "
                "FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise AgentRunError("Agent run not found.")
        return self._row_to_run(row)

    def recent(
        self, *, limit: int = 20, agent_id: str | None = None
    ) -> tuple[AgentRun, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise AgentRunError("Limit must be between 1 and 100.")
        query = (
            "SELECT run_id, agent_id, task, final_text, turns, stopped, provider, "
            "model, input_tokens, output_tokens, started_at, finished_at "
            "FROM agent_runs "
        )
        params: tuple[object, ...]
        if agent_id is None:
            query += "ORDER BY finished_at DESC LIMIT ?"
            params = (limit,)
        else:
            query += "WHERE agent_id = ? ORDER BY finished_at DESC LIMIT ?"
            params = (agent_id, limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._row_to_run(row) for row in rows)

    def steps(self, run_id: str) -> tuple[AgentRunStep, ...]:
        if not isinstance(run_id, str) or IDENTIFIER.fullmatch(run_id) is None:
            raise AgentRunError("Invalid run identifier.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT turn, kind, tool, arguments_json, output "
                "FROM agent_run_steps WHERE run_id = ? ORDER BY turn",
                (run_id,),
            ).fetchall()
        return tuple(
            AgentRunStep(
                turn=int(str(row[0])),
                kind=str(row[1]),
                tool=str(row[2]),
                arguments=json.loads(str(row[3])),
                output=str(row[4]),
            )
            for row in rows
        )

    @staticmethod
    def _row_to_run(row: tuple[object, ...]) -> AgentRun:
        return AgentRun(
            run_id=str(row[0]),
            agent_id=str(row[1]),
            task=str(row[2]),
            final_text=str(row[3]),
            turns=int(str(row[4])),
            stopped=bool(row[5]),
            provider=str(row[6]),
            model=str(row[7]),
            input_tokens=None if row[8] is None else int(str(row[8])),
            output_tokens=None if row[9] is None else int(str(row[9])),
            started_at=parse_timestamp(str(row[10])),
            finished_at=parse_timestamp(str(row[11])),
        )


__all__ = [
    "AgentRun",
    "AgentRunError",
    "AgentRunStep",
    "AgentRunStore",
]
