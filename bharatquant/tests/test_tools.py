from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.agent_runtime import (
    ConversationHistory,
    ModelReply,
    run_agent_loop,
)
from bq.infra.database import migrate
from bq.news import NewsStore, parse_feed
from bq.news_agent import NewsResearchAgent
from bq.tools.base import (
    Tool,
    ToolArgument,
    ToolResult,
    ToolSpec,
    ToolValidationError,
    render_tools_prompt,
)
from bq.tools.control import ResponseTool
from bq.tools.parsing import ToolCallParseError, parse_tool_call
from bq.tools.registry import AgentToolPolicy, ToolRegistry, ToolRunner

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
  <item>
    <title>New sanctions announced after border conflict</title>
    <description>Diplomats react to the sanctions.</description>
    <link>https://example.com/sanctions</link>
    <pubDate>Wed, 25 Nov 2026 18:00:00 +0530</pubDate>
  </item>
</channel></rss>
"""


class FrozenClock:
    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


class EchoTool(Tool):
    spec = ToolSpec(
        name="echo",
        summary="Echo the provided text back.",
        arguments=(ToolArgument("text", "Text to echo.", required=True),),
    )

    def execute(self, args):
        return ToolResult(message=str(args["text"]))


class WriteTool(Tool):
    spec = ToolSpec(
        name="write_state",
        summary="Pretend to write state.",
        arguments=(ToolArgument("value", "A value.", required=True),),
        mutating=True,
    )

    def execute(self, args):
        return ToolResult(message=f"wrote {args['value']}")


def _seed(tmp_path: Path) -> Path:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    NewsStore(path, FrozenClock()).ingest(parse_feed(FEED, ingested_at=NOW))
    return path


# ----------------------------------------------------------------- tool base


def test_tool_spec_rejects_bad_surface() -> None:
    with pytest.raises(ToolValidationError, match="name"):
        ToolSpec(name="Bad Name", summary="x")
    with pytest.raises(ToolValidationError, match="summary"):
        ToolSpec(name="ok", summary="")
    with pytest.raises(ToolValidationError, match="unique"):
        ToolSpec(
            name="ok",
            summary="ok",
            arguments=(
                ToolArgument("a", "first"),
                ToolArgument("a", "second"),
            ),
        )


def test_tool_validates_arguments_and_bounds_result() -> None:
    tool = EchoTool()
    assert tool.run({"text": "hi"}).message == "hi"
    with pytest.raises(ToolValidationError, match="Missing required"):
        tool.run({})
    with pytest.raises(ToolValidationError, match="Unexpected"):
        tool.run({"text": "hi", "extra": "x"})

    class HugeTool(Tool):
        spec = ToolSpec(name="huge", summary="big")

        def execute(self, args):
            return ToolResult(message="x" * 50000)

    result = HugeTool().run({})
    assert result.message.endswith("[truncated]")
    assert len(result.message) < 25000


def test_render_tools_prompt_lists_specs() -> None:
    prompt = render_tools_prompt((EchoTool().spec,))
    assert "### echo" in prompt
    assert "`text`" in prompt


# ------------------------------------------------------------------ registry


def test_registry_registers_and_runs() -> None:
    registry = ToolRegistry((EchoTool(), ResponseTool()))
    runner = ToolRunner(registry, AgentToolPolicy(profile="test", read_only=True))
    assert "echo" in registry
    assert {spec.name for spec in runner.permitted_specs()} == {"echo", "response"}
    out = runner.run("echo", {"text": "hey"})
    assert out["message"] == "hey"
    assert out["break_loop"] is False


def test_policy_blocks_mutation_for_read_only_profile() -> None:
    registry = ToolRegistry((EchoTool(), WriteTool()))
    read_only = ToolRunner(registry, AgentToolPolicy(profile="analyst", read_only=True))
    assert {spec.name for spec in read_only.permitted_specs()} == {"echo"}
    with pytest.raises(Exception, match="read-only"):
        read_only.run("write_state", {"value": "x"})

    writer = ToolRunner(registry, AgentToolPolicy(profile="writer", read_only=False))
    assert {spec.name for spec in writer.permitted_specs()} == {"echo", "write_state"}


def test_policy_blocked_set_is_enforced() -> None:
    registry = ToolRegistry((EchoTool(), WriteTool()))
    runner = ToolRunner(
        registry,
        AgentToolPolicy(
            profile="x", blocked=frozenset({"write_state"}), read_only=False
        ),
    )
    with pytest.raises(Exception, match="not permitted"):
        runner.run("write_state", {"value": "x"})


# ------------------------------------------------------------------- parsing


def test_parse_accepts_plain_fenced_and_embedded_calls() -> None:
    assert parse_tool_call('{"tool_name":"echo","tool_args":{"text":"a"}}') == (
        "echo",
        {"text": "a"},
    )
    fenced = 'Sure:\n```json\n{"tool_name":"echo","tool_args":{"text":"b"}}\n```'
    assert parse_tool_call(fenced) == ("echo", {"text": "b"})
    embedded = 'thinking... {"tool_name":"echo","tool_args":{"text":"c"}} done'
    assert parse_tool_call(embedded) == ("echo", {"text": "c"})


def test_parse_rejects_malformed_or_foreign_objects() -> None:
    with pytest.raises(ToolCallParseError):
        parse_tool_call("no json here")
    with pytest.raises(ToolCallParseError):
        parse_tool_call('{"not":"a tool call"}')
    with pytest.raises(ToolCallParseError):
        parse_tool_call('{"tool_name":"echo","tool_args":[1,2]}')


# --------------------------------------------------------------------- tools


def test_news_tools_read_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed(tmp_path)
    # Keep the ingest step deterministic; the default agent would fetch the live feed.
    monkeypatch.setattr("bq.news_agent.fetch_feed", lambda _url: FEED)
    agent = NewsResearchAgent(path, clock=FrozenClock())
    runner = agent.runner()

    digest = runner.run("news_digest", {"limit": 5})
    assert "recent item(s)" in digest["message"]

    search = runner.run("news_search", {"query": "nifty", "limit": 5})
    assert "Nifty" in search["message"]

    memory = runner.run("news_memory", {"limit": 5})
    assert "memor" in memory["message"]

    ingest = runner.run("news_ingest", {"memory_ttl_days": 30})
    assert "News ingest complete" in ingest["message"]


def test_news_runner_read_only_hides_ingest(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    agent = NewsResearchAgent(path, clock=FrozenClock())
    names = {spec.name for spec in agent.runner(read_only=True).permitted_specs()}
    assert "news_ingest" not in names
    assert "unknown" in names
    assert "response" in names


def test_agent_tool_registry_includes_control_tools(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    agent = NewsResearchAgent(path, clock=FrozenClock())
    registry = agent.tool_registry()
    names = {spec.name for spec in registry.specs()}
    assert {
        "news_search",
        "news_memory",
        "news_digest",
        "news_ingest",
        "response",
        "unknown",
    } <= names


# ------------------------------------------------------------------ loop


def test_loop_runs_tools_until_response(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    agent = NewsResearchAgent(path, clock=FrozenClock())
    runner = agent.runner()

    script = [
        ModelReply(text="", tool_name="news_digest", tool_args={"limit": 3}),
        ModelReply(text="", tool_name="response", tool_args={"text": "Done."}),
    ]

    def model_turn(_task, _history):
        return script.pop(0)

    outcome = run_agent_loop(
        task="Summarize news", runner=runner, model_turn=model_turn
    )
    assert outcome.stopped is True
    assert outcome.final_text == "Done."
    assert outcome.tool_calls == ("news_digest", "response")


def test_loop_parses_tool_call_from_text(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    runner = NewsResearchAgent(path, clock=FrozenClock()).runner()

    replies = [
        ModelReply(text='{"tool_name":"news_search","tool_args":{"query":"rbi"}}'),
        ModelReply(text='{"tool_name":"response","tool_args":{"text":"ok"}}'),
    ]

    def model_turn(_task, _history):
        return replies.pop(0)

    outcome = run_agent_loop(task="find rbi", runner=runner, model_turn=model_turn)
    assert outcome.stopped
    assert outcome.tool_calls == ("news_search", "response")


def test_loop_returns_plain_text_as_answer(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    runner = NewsResearchAgent(path, clock=FrozenClock()).runner()

    outcome = run_agent_loop(
        task="hi",
        runner=runner,
        model_turn=lambda _t, _h: ModelReply(text="Just answering."),
    )
    assert outcome.final_text == "Just answering."
    assert outcome.stopped is False


def test_loop_survives_blocked_tool_then_finishes(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    runner = NewsResearchAgent(path, clock=FrozenClock()).runner(read_only=True)
    calls = {"n": 0}

    def model_turn(_task, history):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelReply(text="", tool_name="news_ingest", tool_args={})
        # The blocked attempt must be visible in history.
        assert any("blocked" in entry.content for entry in history)
        return ModelReply(text="", tool_name="response", tool_args={"text": "ok"})

    outcome = run_agent_loop(task="ingest", runner=runner, model_turn=model_turn)
    assert outcome.final_text == "ok"
    assert outcome.tool_calls == ("news_ingest", "response")


def test_loop_enforces_bounds() -> None:
    registry = ToolRegistry((EchoTool(),))
    runner = ToolRunner(registry, AgentToolPolicy(profile="x"))
    history = ConversationHistory()

    def looper(_task, _history):
        return ModelReply(text="", tool_name="echo", tool_args={"text": "x"})

    outcome = run_agent_loop(
        task="loop forever", runner=runner, model_turn=looper, max_turns=3
    )
    assert outcome.turns == 3
    assert outcome.stopped is False
    # History stays bounded.
    for index in range(200):
        history.add("tool", f"entry {index}")
    assert len(history.entries) <= 40
    assert history.summary


def test_tools_api_lists_and_runs_with_auth(tmp_path: Path) -> None:
    import asyncio

    import httpx

    from bq.api import ALLOWED_ORIGIN, create_app
    from bq.auth import OwnerAuth

    path = _seed(tmp_path)
    auth = OwnerAuth(path)
    app = create_app(database_path=path)
    password = "synthetic owner password"

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            assert (await client.get("/api/v1/tools/research")).status_code == 401
            auth.setup(password)
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": password},
            )
            csrf = login.json()["csrf_token"]

            listing = await client.get("/api/v1/tools/research")
            assert listing.status_code == 200
            assert listing.headers["cache-control"] == "no-store"
            body = listing.json()
            assert body["agent_id"] == "research"
            assert body["read_only"] is False
            names = {tool["name"] for tool in body["tools"]}
            assert {
                "news_search",
                "news_memory",
                "news_digest",
                "news_ingest",
                "stock_quote",
                "stock_download",
                "web_fetch",
                "response",
            } <= names

            # Missing CSRF -> 403.
            blocked = await client.post(
                "/api/v1/tools/research/run",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"name": "news_digest", "arguments": {"limit": 3}},
            )
            assert blocked.status_code == 403

            ok = await client.post(
                "/api/v1/tools/research/run",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json={"name": "news_search", "arguments": {"query": "nifty"}},
            )
            assert ok.status_code == 200
            assert "Nifty" in ok.json()["message"]

            bad_arg = await client.post(
                "/api/v1/tools/research/run",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json={"name": "news_search", "arguments": {"nope": 1}},
            )
            assert bad_arg.status_code == 422

            unknown = await client.post(
                "/api/v1/tools/research/run",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json={"name": "does_not_exist", "arguments": {}},
            )
            assert unknown.status_code == 422

    asyncio.run(check())
