"""Tests for the model-driven research agent, using a fake model."""

import json
from datetime import UTC, datetime
from pathlib import Path

from bq.agent_runtime import AgentStep
from bq.infra.database import migrate
from bq.llm_result import FunctionCall, LLMResult, LLMUsage
from bq.llm_service import CompletionOutcome
from bq.news import NewsStore, parse_feed
from bq.research_agent import ResearchAgent
from bq.settings import ApiKeyStatus, AppConfig, Provider

NOW = datetime(2026, 11, 25, 22, 0, tzinfo=UTC)

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Pulse</title>
  <item>
    <title>RBI holds repo rate, flags inflation risk</title>
    <description>The monetary policy committee kept the policy rate unchanged.</description>
    <link>https://example.com/rbi-policy</link>
    <pubDate>Wed, 25 Nov 2026 20:00:00 +0530</pubDate>
  </item>
  <item>
    <title>Nifty closes at record high as FII flows turn positive</title>
    <description>Sensex and Nifty gained on strong buying.</description>
    <link>https://example.com/nifty-record</link>
    <pubDate>Wed, 25 Nov 2026 19:00:00 +0530</pubDate>
  </item>
</channel></rss>
"""


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _fake_config() -> AppConfig:
    return AppConfig(
        provider=Provider.GEMINI,
        model_name="gemini-2.5-pro",
        news_feed_url="https://example.com/feed",
        news_memory_ttl_days=30,
        api_keys=(ApiKeyStatus(Provider.GEMINI, True, "********abcd"),),
        updated_at=NOW,
    )


class _FakeStore:
    def load(self) -> AppConfig:
        return _fake_config()


class ScriptedService:
    """Stands in for LLMService: returns pre-scripted replies in order."""

    def __init__(self, replies: list[LLMResult]) -> None:
        self.replies = replies
        self.calls = 0
        self.store = _FakeStore()

    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        task_id: str = "research.agent",
        task_version: str = "v1",
    ) -> CompletionOutcome:

        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        config = _fake_config()
        return CompletionOutcome(
            provider=config.provider, model=config.model_name, result=reply
        )


def tool_call(name: str, args: dict[str, object]) -> LLMResult:
    payload = json.dumps({"tool_name": name, "tool_args": args})
    return LLMResult(text=payload, usage=LLMUsage(10, 5))


def _seed(tmp_path: Path) -> Path:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    NewsStore(path, FrozenClock()).ingest(parse_feed(FEED, ingested_at=NOW))
    return path


def test_agent_runs_tools_then_responds(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    service = ScriptedService(
        [
            tool_call("news_search", {"query": "rbi"}),
            tool_call("response", {"text": "RBI held the repo rate."}),
        ]
    )
    agent = ResearchAgent(
        path, clock=FrozenClock(), feed_url="https://example.com/feed", service=service
    )
    run = agent.run("What did the RBI do?")
    assert run.stopped is True
    assert run.final_text == "RBI held the repo rate."
    assert run.tool_calls == ("news_search", "response")
    assert run.turns == 2
    assert [s.tool for s in run.steps] == ["news_search", "response"]
    assert run.input_tokens == 20  # 10 per call, two calls
    assert run.output_tokens == 10


def test_agent_step_records_arguments_and_output(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    service = ScriptedService(
        [
            tool_call("news_digest", {"limit": 3}),
            tool_call("response", {"text": "done"}),
        ]
    )
    run = ResearchAgent(
        path, clock=FrozenClock(), feed_url="https://x/feed", service=service
    ).run("digest please")
    first = run.steps[0]
    assert first.tool == "news_digest"
    assert first.arguments == {"limit": 3}
    assert "News digest" in first.output


def test_agent_handles_blocked_and_unknown_tools(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    service = ScriptedService(
        [
            tool_call("does_not_exist", {}),
            tool_call("response", {"text": "recovered"}),
        ]
    )
    run = ResearchAgent(
        path, clock=FrozenClock(), feed_url="https://x/feed", service=service
    ).run("try something")
    assert run.final_text == "recovered"
    assert run.steps[0].kind == "error"


def test_agent_uses_native_function_calls(tmp_path: Path) -> None:
    path = _seed(tmp_path)

    class NativeService(ScriptedService):
        def complete(
            self,
            *,
            prompt: str,
            system: str = "",
            task_id: str = "research.agent",
            task_version: str = "v1",
        ) -> CompletionOutcome:

            self.calls += 1
            if self.calls == 1:
                reply = LLMResult(
                    text="",
                    function_calls=(
                        FunctionCall(name="news_digest", arguments={"limit": 2}),
                    ),
                )
            else:
                reply = LLMResult(
                    text="",
                    function_calls=(
                        FunctionCall(
                            name="response", arguments={"text": "native done"}
                        ),
                    ),
                )
            config = _fake_config()
            return CompletionOutcome(
                provider=config.provider, model=config.model_name, result=reply
            )

    service = NativeService([])
    run = ResearchAgent(
        path, clock=FrozenClock(), feed_url="https://x/feed", service=service
    ).run("native")
    assert run.final_text == "native done"
    assert run.tool_calls == ("news_digest", "response")


def test_agent_step_to_dict_roundtrip() -> None:
    step = AgentStep(1, "tool", "news_search", {"query": "x"}, "out")
    assert step.to_dict() == {
        "turn": 1,
        "kind": "tool",
        "tool": "news_search",
        "arguments": {"query": "x"},
        "output": "out",
    }
