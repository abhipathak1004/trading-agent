"""The Research Lab runner: journal, cursor reads, single-flight and cancel."""

import asyncio

import pytest

from bq.research_lab.runner import (
    ResearchBusy,
    ResearchRunner,
    ResearchRunnerError,
)


def test_events_are_journalled_with_monotonic_seq(tmp_path) -> None:
    runner = ResearchRunner(tmp_path)
    emit = runner.emitter("run_a")
    emit("step", {"name": "queued"})
    emit("tool_call", {"tool": "backtest"})
    events = runner.read_events("run_a")
    assert [item["seq"] for item in events] == [1, 2]
    assert [item["type"] for item in events] == ["step", "tool_call"]


def test_cursor_reads_replay_only_new_events(tmp_path) -> None:
    runner = ResearchRunner(tmp_path)
    emit = runner.emitter("run_a")
    for index in range(5):
        emit("step", {"name": f"step-{index}"})
    assert [item["seq"] for item in runner.read_events("run_a", after=2)] == [3, 4, 5]
    assert runner.read_events("run_a", after=99) == []


def test_unknown_event_type_is_refused(tmp_path) -> None:
    runner = ResearchRunner(tmp_path)
    with pytest.raises(ResearchRunnerError):
        runner.append("run_a", {"type": "invented"})


def test_one_run_per_session(tmp_path) -> None:
    runner = ResearchRunner(tmp_path)

    async def scenario() -> None:
        gate = asyncio.Event()

        async def slow(emit) -> dict[str, object]:
            await gate.wait()
            return {"ok": True}

        await runner.start(session_id="s1", run_id="run_a", work=slow)
        with pytest.raises(ResearchBusy):
            await runner.start(session_id="s1", run_id="run_b", work=slow)
        gate.set()
        await runner.wait("s1")

    asyncio.run(scenario())


def test_cancel_writes_a_terminal_event(tmp_path) -> None:
    runner = ResearchRunner(tmp_path)

    async def scenario() -> None:
        async def forever(emit) -> dict[str, object]:
            while True:
                await asyncio.sleep(10)
            return {}

        await runner.start(session_id="s1", run_id="run_a", work=forever)
        assert runner.is_running("s1") is True
        assert await runner.cancel("s1") is True
        assert runner.is_running("s1") is False
        events = runner.read_events("run_a")
        assert events[-1]["type"] == "cancelled"
        assert events[-1]["done"] is True

    asyncio.run(scenario())
