"""Native tool calling: schemas, the Bedrock round trip, and the agent loop.

The gap this closes: ``ModelClient.complete()`` had no ``tools`` parameter, so
every agent injected tool descriptions as prose and parsed calls back out of
the text. Bedrock's ``Converse`` needs a real ToolConfiguration, and it needs
the assistant's ``toolUse`` block plus the matching ``toolResult`` to appear in
the conversation — otherwise the model has no record of its call and asks
again on every turn.
"""

import json
from pathlib import Path

import httpx
import pytest

from bq.agent_runtime import ModelReply, run_agent_loop
from bq.llm_client import (
    BedrockClient,
    OpenAICompatClient,
    ToolCallRequest,
    ToolResult,
    _bedrock_tool_config,
)
from bq.tools.base import ToolArgument, ToolSpec
from bq.tools.registry import AgentToolPolicy, ToolRegistry, ToolRunner

WEATHER = ToolSpec(
    name="read_quote",
    summary="Read the last traded price for a ticker.",
    arguments=(
        ToolArgument(name="ticker", description="NSE symbol", required=True),
        ToolArgument(name="limit", description="rows", kind=int),
        ToolArgument(name="adjusted", description="adjust for splits", kind=bool),
    ),
)

TOOL_USE_REPLY = {
    "output": {
        "message": {
            "role": "assistant",
            "content": [
                {"text": "Let me check that."},
                {
                    "toolUse": {
                        "toolUseId": "call-1",
                        "name": "read_quote",
                        "input": {"ticker": "AAPL"},
                    }
                },
            ],
        }
    },
    "stopReason": "tool_use",
    "usage": {"inputTokens": 50, "outputTokens": 20},
}


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# declaring a tool's schema
# ---------------------------------------------------------------------------


def test_tool_spec_renders_json_schema() -> None:
    schema = WEATHER.input_schema()
    assert schema["type"] == "object"
    assert schema["properties"]["ticker"] == {
        "type": "string",
        "description": "NSE symbol",
    }
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["properties"]["adjusted"]["type"] == "boolean"
    assert schema["required"] == ["ticker"]
    # The runner validates against this same spec, so nothing else may be sent.
    assert schema["additionalProperties"] is False


def test_a_tool_with_no_arguments_still_declares_an_object() -> None:
    spec = ToolSpec(name="ping", summary="No arguments.")
    assert spec.input_schema() == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    function = spec.as_function_schema()
    assert function["name"] == "ping"
    assert function["description"] == "No arguments."
    assert function["parameters"] == spec.input_schema()


# ---------------------------------------------------------------------------
# Bedrock: the tool configuration
# ---------------------------------------------------------------------------


def test_bedrock_tool_config_translates_the_schema() -> None:
    config = _bedrock_tool_config([WEATHER.as_function_schema()])
    assert config is not None
    spec = config["tools"][0]["toolSpec"]
    assert spec["name"] == "read_quote"
    assert spec["description"].startswith("Read the last traded price")
    # Converse nests the schema one level deeper than OpenAI.
    assert spec["inputSchema"]["json"]["type"] == "object"
    assert spec["inputSchema"]["json"]["required"] == ["ticker"]


def test_bedrock_tool_config_is_absent_rather_than_empty() -> None:
    # Converse rejects an empty tool list, so None must mean "do not send it".
    assert _bedrock_tool_config(None) is None
    assert _bedrock_tool_config([]) is None
    assert _bedrock_tool_config([{"description": "nameless"}]) is None


def test_bedrock_returns_a_tool_use_as_a_function_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=TOOL_USE_REPLY)

    result = BedrockClient(transport=_mock(handler)).complete(
        model="us.anthropic.claude-sonnet-4-6",
        api_key="k" * 16,
        system="",
        prompt="price of AAPL?",
        tools=[WEATHER.as_function_schema()],
    )
    assert result.text == "Let me check that."
    assert result.finish_reason == "tool_calls"  # Converse's `tool_use`
    assert len(result.function_calls) == 1
    call = result.function_calls[0]
    assert call.name == "read_quote"
    assert call.arguments == {"ticker": "AAPL"}
    assert call.call_id == "call-1"


def test_bedrock_sends_the_tool_configuration() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=TOOL_USE_REPLY)

    BedrockClient(transport=_mock(handler)).complete(
        model="m",
        api_key="k" * 16,
        system="",
        prompt="hi",
        tools=[WEATHER.as_function_schema()],
    )
    body = seen["body"]
    assert body["toolConfig"]["tools"][0]["toolSpec"]["name"] == "read_quote"


def test_bedrock_replays_the_call_and_result_in_order() -> None:
    """The assistant toolUse block must precede the toolResult user message."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "output": {"message": {"content": [{"text": "Done."}]}},
                "stopReason": "end_turn",
            },
        )

    BedrockClient(transport=_mock(handler)).complete(
        model="m",
        api_key="k" * 16,
        system="",
        prompt="continue",
        tool_call=ToolCallRequest(
            call_id="call-1", name="read_quote", arguments={"ticker": "AAPL"}
        ),
        tool_result=ToolResult(call_id="call-1", name="read_quote", content="189.20"),
    )
    messages = seen["body"]["messages"]
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"][0]["toolUse"] == {
        "toolUseId": "call-1",
        "name": "read_quote",
        "input": {"ticker": "AAPL"},
    }
    # Converse requires the result in a *user* message.
    assert messages[1]["role"] == "user"
    assert messages[1]["content"][0]["toolResult"] == {
        "toolUseId": "call-1",
        "content": [{"text": "189.20"}],
    }
    assert messages[2] == {"role": "user", "content": [{"text": "continue"}]}


def test_a_tool_only_answer_is_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {
                    "message": {
                        "content": [
                            {"toolUse": {"toolUseId": "c", "name": "t", "input": {}}}
                        ]
                    }
                },
                "stopReason": "tool_use",
            },
        )

    result = BedrockClient(transport=_mock(handler)).complete(
        model="m", api_key="k" * 16, system="", prompt="hi"
    )
    assert result.text == ""
    assert result.function_calls[0].name == "t"


# ---------------------------------------------------------------------------
# the OpenAI-compatible and Gemini shapes
# ---------------------------------------------------------------------------


def test_openai_compat_sends_tools_and_replays_the_exchange() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    client = OpenAICompatClient("https://api.openai.com/v1", transport=_mock(handler))
    client.complete(
        model="m",
        api_key="k" * 16,
        system="sys",
        prompt="go",
        tools=[WEATHER.as_function_schema()],
        tool_call=ToolCallRequest(call_id="c1", name="read_quote", arguments={"a": 1}),
        tool_result=ToolResult(call_id="c1", name="read_quote", content="189.20"),
    )
    body = seen["body"]
    assert body["tools"][0] == {
        "type": "function",
        "function": WEATHER.as_function_schema(),
    }
    messages = body["messages"]
    assert messages[0] == {"role": "system", "content": "sys"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["id"] == "c1"
    # OpenAI requires the tool message to name the call it answers.
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "c1"
    assert messages[3] == {"role": "user", "content": "go"}


def test_a_tool_result_is_bounded() -> None:
    result = ToolResult(call_id="c", name="t", content="x" * 100_000)
    assert len(result.content) == 24_000


# ---------------------------------------------------------------------------
# the agent loop
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner(tmp_path: Path) -> ToolRunner:
    """A runner holding one real tool, so policy and validation both run."""
    from bq.tools.base import Tool, ToolResult

    class Echo(Tool):
        spec = WEATHER

        def execute(self, args):  # noqa: ANN001, ANN201
            return ToolResult(message=f"quote for {args.get('ticker')}: 189.20")

    return ToolRunner(
        ToolRegistry([Echo()]), AgentToolPolicy(profile="test", read_only=True)
    )


def test_loop_stages_the_call_and_result_for_native_replay(runner: ToolRunner) -> None:
    """One tool call, then an answer: the loop must hand back both halves."""
    seen_pending: list[dict[str, object]] = []
    pending: dict[str, object] = {"call": None, "result": None}

    def turn(task: str, history):  # noqa: ANN001, ANN202
        # Nothing staged on the first turn.
        seen_pending.append(dict(pending))
        if len(seen_pending) == 1:
            return ModelReply(
                text="checking",
                tool_name="read_quote",
                tool_args={"ticker": "AAPL"},
                tool_call_id="c1",
            )
        return ModelReply(text="AAPL is 189.20")

    # Same contract the real closure exposes.
    turn.pending = pending  # type: ignore[attr-defined]

    result = run_agent_loop(task="price?", runner=runner, model_turn=turn, max_turns=4)

    assert result.final_text == "AAPL is 189.20"
    assert result.tool_calls == ("read_quote",)
    # First turn had nothing; the second turn received the call and its result.
    assert seen_pending[0]["call"] is None
    staged = seen_pending[1]
    assert isinstance(staged["call"], ToolCallRequest)
    assert staged["call"].call_id == "c1"
    assert isinstance(staged["result"], ToolResult)
    assert "189.20" in staged["result"].content


def test_loop_ignores_calls_parsed_from_prose(runner: ToolRunner) -> None:
    """A call recovered from text has no id, so there is no protocol to replay."""
    staged: list[dict[str, object]] = []
    pending: dict[str, object] = {"call": None, "result": None}

    def turn(task: str, history):  # noqa: ANN001, ANN202
        staged.append(dict(pending))
        if len(staged) == 1:
            # A native-looking call with no call_id.
            return ModelReply(
                text="", tool_name="read_quote", tool_args={"ticker": "X"}
            )
        return ModelReply(text="done")

    turn.pending = pending  # type: ignore[attr-defined]

    run_agent_loop(task="t", runner=runner, model_turn=turn, max_turns=3)
    assert staged[1]["call"] is None


def test_loop_works_for_a_turn_without_a_pending_slot(runner: ToolRunner) -> None:
    """A provider with no tool protocol must keep working from text history."""

    def turn(task: str, history):  # noqa: ANN001, ANN202
        if not any(getattr(entry, "role", "") == "tool" for entry in history):
            return ModelReply(
                text="", tool_name="read_quote", tool_args={"ticker": "AAPL"}
            )
        return ModelReply(text="finished")

    result = run_agent_loop(task="t", runner=runner, model_turn=turn, max_turns=3)
    assert result.final_text == "finished"
