"""Extract reasoning, text and tool calls from provider responses.

Adapted from Agent Zero's ``helpers/litellm_transport.py`` parsing helpers and
``helpers/llm_result.py`` (MIT License, Copyright (c) 2025 Agent Zero, s.r.o,
https://github.com/agent0ai/agent-zero). Reimplemented without LiteLLM/OpenAI
object coupling so it operates on the plain JSON both our clients already get.

Two jobs:

* ``split_thinking`` — separate ``<think>…</think>`` / ``<reasoning>…</reasoning>``
  blocks from visible text. Native-reasoning models put reasoning in their own
  field; text-mode models inline it, and mixing the two into one answer would
  corrupt the stored output.
* ``extract_tool_calls`` — normalise the several tool-call shapes providers emit
  (OpenAI ``tool_calls``, Gemini ``functionCall``, legacy ``function_call``) into
  one list of ``(name, arguments)`` pairs.
"""

import json
from collections.abc import Mapping

THINKING_PAIRS: tuple[tuple[str, str], ...] = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<reasoning>", "</reasoning>"),
)


def split_thinking(text: str) -> tuple[str, str]:
    """Return ``(visible_text, reasoning)``.

    Reasoning inside recognised tags is removed from the visible text and
    returned separately. Multiple blocks are concatenated with a blank line.
    Unterminated tags are treated as visible text: a truncated stream is not a
    reliable signal that everything after it was reasoning.
    """
    if not isinstance(text, str) or not text:
        return "", ""
    reasoning_parts: list[str] = []
    remaining = text
    for opening, closing in THINKING_PAIRS:
        while True:
            start = remaining.find(opening)
            if start == -1:
                break
            end = remaining.find(closing, start + len(opening))
            if end == -1:
                # Unterminated: leave the rest visible.
                break
            reasoning_parts.append(remaining[start + len(opening) : end].strip())
            remaining = remaining[:start] + remaining[end + len(closing) :]
    visible = remaining.strip()
    reasoning = "\n\n".join(part for part in reasoning_parts if part)
    return visible, reasoning


def _tool_call_entry(
    name: object, arguments: object, call_id: object = ""
) -> dict[str, object] | None:
    if not isinstance(name, str) or not name.strip():
        return None
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            parsed = {"arguments": arguments}
        arguments = parsed
    if not isinstance(arguments, Mapping):
        arguments = {"arguments": arguments}
    return {
        "name": name.strip(),
        "arguments": dict(arguments),
        "call_id": str(call_id or ""),
    }


def extract_tool_calls(message: Mapping[str, object]) -> list[dict[str, object]]:
    """Normalise tool calls from an OpenAI-compatible ``message`` object."""
    calls: list[dict[str, object]] = []
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for item in tool_calls:
            if not isinstance(item, Mapping):
                continue
            function = item.get("function")
            if isinstance(function, Mapping):
                entry = _tool_call_entry(
                    function.get("name"),
                    function.get("arguments"),
                    item.get("id"),
                )
                if entry:
                    calls.append(entry)
    legacy = message.get("function_call")
    if isinstance(legacy, Mapping):
        entry = _tool_call_entry(legacy.get("name"), legacy.get("arguments"))
        if entry:
            calls.append(entry)
    return calls


def extract_gemini_calls(candidates: object) -> list[dict[str, object]]:
    """Normalise ``functionCall`` parts from a Gemini candidate."""
    calls: list[dict[str, object]] = []
    if not isinstance(candidates, list):
        return calls
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        content = candidate.get("content")
        if not isinstance(content, Mapping):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            function = part.get("functionCall")
            if isinstance(function, Mapping):
                entry = _tool_call_entry(function.get("name"), function.get("args", {}))
                if entry:
                    calls.append(entry)
    return calls


def map_finish_reason(raw: object) -> str:
    """Normalise provider stop reasons to OpenAI-style names our code checks."""
    if not isinstance(raw, str):
        return ""
    value = raw.strip().upper()
    mapping = {
        "STOP": "stop",
        "LENGTH": "length",
        "MAX_TOKENS": "max_tokens",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "BLOCKLIST": "content_filter",
        "PROHIBITED_CONTENT": "content_filter",
        "TOOL_CALLS": "tool_calls",
        "FUNCTION_CALL": "tool_calls",
        "END_TURN": "stop",
    }
    return mapping.get(value, value.lower())
