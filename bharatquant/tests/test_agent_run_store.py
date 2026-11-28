from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.agent_run_store import AgentRunError, AgentRunStep, AgentRunStore
from bq.infra.database import migrate

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _store(tmp_path: Path) -> AgentRunStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return AgentRunStore(path, FrozenClock())


def _steps() -> tuple[AgentRunStep, ...]:
    return (
        AgentRunStep(1, "tool", "news_search", {"query": "rbi"}, "found"),
        AgentRunStep(2, "tool", "response", {"text": "done"}, "done"),
    )


def test_round_trip_and_steps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.record(
        agent_id="research",
        task="What did the RBI do?",
        final_text="RBI held rates.",
        turns=2,
        stopped=True,
        provider="gemini",
        model="gemini-2.5-pro",
        input_tokens=20,
        output_tokens=10,
        steps=_steps(),
        started_at=NOW,
    )
    assert run.run_id
    assert run.stopped is True
    assert store.get(run.run_id).task == "What did the RBI do?"
    assert store.steps(run.run_id) == _steps()
    assert store.recent()[0].run_id == run.run_id


def test_unknown_and_invalid_run_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AgentRunError, match="not found"):
        store.get("missing")
    with pytest.raises(AgentRunError, match="Invalid run identifier"):
        store.get("bad id")


def test_record_rejects_oversized_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AgentRunError, match="Task"):
        store.record(
            agent_id="research",
            task="x" * 4001,
            final_text="ok",
            turns=1,
            stopped=True,
            provider="",
            model="",
            input_tokens=None,
            output_tokens=None,
            steps=(),
            started_at=NOW,
        )
