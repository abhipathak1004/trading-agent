"""The canonical result of one model call.

Adapted from Agent Zero's ``helpers/llm_result.py`` (MIT License,
Copyright (c) 2025 Agent Zero, s.r.o, https://github.com/agent0ai/agent-zero).
Reduced to what BharatQuant needs: text, reasoning, usage, finish reason and
function calls. The upstream version also carries OpenAI "Responses API" state
(response ids, input/output items, capability metadata); we do not use that
transport, so those fields are omitted rather than carried dead.

Why this exists: one typed object per call lets the gateway, ledger and UI all
read the same fields, and the ``truncated`` flag tells callers the provider cut
the answer short — which must not be mistaken for a complete answer.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "content_filter"})


@dataclass(frozen=True, slots=True)
class FunctionCall:
    name: str
    arguments: dict[str, object]
    call_id: str = ""


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token usage. ``reported`` is False when the provider omitted it."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    reported: bool = True

    def __post_init__(self) -> None:
        for value in (self.input_tokens, self.output_tokens):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError("Token counts must be nonnegative integers.")

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    reasoning: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    finish_reason: str = ""
    function_calls: tuple[FunctionCall, ...] = ()
    raw: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("Result text must be a string.")
        if not isinstance(self.reasoning, str):
            raise ValueError("Result reasoning must be a string.")

    @property
    def input_tokens(self) -> int | None:
        return self.usage.input_tokens

    @property
    def output_tokens(self) -> int | None:
        return self.usage.output_tokens

    @property
    def truncated(self) -> bool:
        """True when the provider stopped because the output limit was reached."""
        return self.finish_reason.lower() in TRUNCATED_FINISH_REASONS

    def function_calls_text(self) -> str:
        """Render function calls as the tool-call JSON our parser understands."""
        calls = [
            {"tool_name": call.name, "tool_args": call.arguments}
            for call in self.function_calls
        ]
        if not calls:
            return ""
        if len(calls) == 1:
            return json.dumps(calls[0], ensure_ascii=False)
        return json.dumps(
            {"tool_name": "parallel_tool_calls", "tool_args": {"calls": calls}},
            ensure_ascii=False,
        )


def parse_arguments(raw: object) -> dict[str, object]:
    """Coerce a provider's ``arguments`` field (str or dict) into a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {"arguments": raw}
        return parsed if isinstance(parsed, dict) else {"arguments": parsed}
    return {"arguments": raw}


def parse_usage(raw: object) -> LLMUsage:
    """Read usage from the shapes the provider APIs emit.

    Bedrock's ``Converse`` reports camelCase ``inputTokens``/``outputTokens``,
    which is distinct from Gemini's ``*TokenCount`` and OpenAI's
    ``*_tokens``; missing it would mark every Bedrock call as unreported.
    """
    if not isinstance(raw, Mapping):
        return LLMUsage(reported=False)
    input_tokens = _first_int(
        raw,
        (
            "promptTokenCount",
            "prompt_tokens",
            "input_tokens",
            "inputTokenCount",
            "inputTokens",
        ),
    )
    output_tokens = _first_int(
        raw,
        (
            "candidatesTokenCount",
            "completion_tokens",
            "output_tokens",
            "outputTokenCount",
            "outputTokens",
        ),
    )
    reported = input_tokens is not None or output_tokens is not None
    return LLMUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reported=reported,
    )


def _first_int(raw: Mapping[object, object], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None
