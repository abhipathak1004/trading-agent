"""Tests for the LLM helpers adapted from Agent Zero (MIT)."""

import httpx
import pytest

from bq.dirty_json import DirtyJson
from bq.llm_extract import (
    extract_gemini_calls,
    extract_tool_calls,
    map_finish_reason,
    split_thinking,
)
from bq.llm_result import (
    FunctionCall,
    LLMResult,
    LLMUsage,
    parse_arguments,
    parse_usage,
)
from bq.llm_retry import (
    RateLimiter,
    backoff_delay,
    is_transient,
    run_with_retry,
)
from bq.llm_tokens import approximate_tokens, count_tokens, trim_to_tokens
from bq.tools.parsing import ToolCallParseError, parse_tool_call

# --------------------------------------------------------------- llm_result


def test_usage_and_totals() -> None:
    usage = LLMUsage(input_tokens=10, output_tokens=5)
    assert usage.total_tokens == 15
    with pytest.raises(ValueError):
        LLMUsage(input_tokens=-1)


def test_parse_usage_handles_both_wire_shapes() -> None:
    gemini = parse_usage({"promptTokenCount": 7, "candidatesTokenCount": 2})
    assert gemini.input_tokens == 7 and gemini.output_tokens == 2 and gemini.reported
    openai = parse_usage({"prompt_tokens": 3, "completion_tokens": 4})
    assert openai.input_tokens == 3 and openai.output_tokens == 4
    missing = parse_usage({})
    assert missing.reported is False


def test_truncated_and_function_calls_text() -> None:
    result = LLMResult(text="x", finish_reason="length")
    assert result.truncated is True
    assert LLMResult(text="x", finish_reason="stop").truncated is False
    call = LLMResult(
        text="",
        function_calls=(FunctionCall(name="news_search", arguments={"query": "rbi"}),),
    )
    assert "news_search" in call.function_calls_text()


def test_parse_arguments_handles_str_and_dict() -> None:
    assert parse_arguments('{"a": 1}') == {"a": 1}
    assert parse_arguments({"a": 1}) == {"a": 1}
    assert parse_arguments("not json") == {"arguments": "not json"}


# --------------------------------------------------------------- extraction


def test_split_thinking_separates_reasoning() -> None:
    visible, reasoning = split_thinking("<think>plan</think>Answer here")
    assert visible == "Answer here"
    assert reasoning == "plan"
    # Unterminated tag stays visible.
    visible2, _ = split_thinking("<think>oops no close")
    assert visible2.startswith("<think>")


def test_extract_openai_tool_calls() -> None:
    message = {
        "tool_calls": [
            {
                "id": "call_1",
                "function": {"name": "news_search", "arguments": '{"query":"rbi"}'},
            }
        ]
    }
    calls = extract_tool_calls(message)
    assert calls == [
        {"name": "news_search", "arguments": {"query": "rbi"}, "call_id": "call_1"}
    ]


def test_extract_gemini_calls() -> None:
    candidates = [
        {"content": {"parts": [{"functionCall": {"name": "news_digest", "args": {}}}]}}
    ]
    calls = extract_gemini_calls(candidates)
    assert calls[0]["name"] == "news_digest"


def test_finish_reason_mapping() -> None:
    assert map_finish_reason("STOP") == "stop"
    assert map_finish_reason("MAX_TOKENS") in {"length", "max_tokens"}
    assert map_finish_reason("SAFETY") == "content_filter"


# -------------------------------------------------------------------- retry


def test_is_transient_classification() -> None:
    request = httpx.Request("POST", "https://example.com")
    assert is_transient(httpx.ConnectTimeout("t")) is True
    assert (
        is_transient(
            httpx.HTTPStatusError(
                "429", request=request, response=httpx.Response(429, request=request)
            )
        )
        is True
    )
    assert (
        is_transient(
            httpx.HTTPStatusError(
                "400", request=request, response=httpx.Response(400, request=request)
            )
        )
        is False
    )


def test_run_with_retry_retries_then_succeeds() -> None:
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom")
        return "ok"

    assert run_with_retry(flaky, sleep=lambda _s: None) == "ok"
    assert attempts["n"] == 3


def test_run_with_retry_stops_on_non_transient() -> None:
    attempts = {"n": 0}

    def bad() -> str:
        attempts["n"] += 1
        raise ValueError("nope")

    with pytest.raises(ValueError):
        run_with_retry(bad, sleep=lambda _s: None)
    assert attempts["n"] == 1


def test_backoff_is_capped() -> None:
    assert backoff_delay(1) == 1.5
    assert backoff_delay(10) == 30.0


def test_rate_limiter_window() -> None:
    limiter = RateLimiter(60, {"requests": 2})
    limiter.add(now=0, requests=1)
    limiter.add(now=0, requests=1)
    assert limiter.would_exceed(now=1, requests=1) is True
    assert limiter.would_exceed(now=61, requests=1) is False


# ------------------------------------------------------------------- tokens


def test_token_estimation_and_trimming() -> None:
    assert count_tokens("") == 0
    assert approximate_tokens("x" * 4000) > 0
    trimmed = trim_to_tokens("word " * 1000, 50)
    assert len(trimmed) < 5000 and trimmed.endswith("...")


# ------------------------------------------------------------ dirty recovery


def test_dirty_json_recovers_near_miss() -> None:
    parsed = DirtyJson.parse_string(
        "{tool_name: 'news_search', tool_args: {query: 'rbi'}}"
    )
    assert parsed["tool_name"] == "news_search"
    assert parsed["tool_args"]["query"] == "rbi"


def test_strict_parser_rejects_but_recovery_accepts() -> None:
    malformed = "{tool_name: 'news_search', tool_args: {query: 'rbi'}}"
    with pytest.raises(ToolCallParseError):
        parse_tool_call(malformed)
    name, args = parse_tool_call(malformed, recover=True)
    assert name == "news_search"
    assert args == {"query": "rbi"}
