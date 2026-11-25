"""Parse a tool call out of model text.

Models do not always emit a clean function call: text-mode models wrap the
call in prose, fenced code, or a slightly malformed JSON object. Agent Zero
handles this with a large, forgiving extractor. BharatQuant needs a much smaller
version because our models are only asked for a single JSON object and we
validate strictly afterwards.

What this accepts, in order:
1. The whole response is a JSON object with ``tool_name`` + ``tool_args``.
2. One fenced ```json block containing such an object.
3. The first balanced ``{...}`` object in the text that looks like a tool call.

Anything else is rejected. Values are never repaired: we parse strict JSON and
fail with a typed error, so a malformed call cannot quietly become a different
call. This is deliberately stricter than the upstream extractor.
"""

import json
import re
from collections.abc import Iterator

MAX_SCAN_CHARS = 64 * 1024
MAX_NESTING = 32
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


class ToolCallParseError(ValueError):
    pass


def _object_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield balanced ``{...}`` spans, ignoring braces inside JSON strings."""
    depth = 0
    start = -1
    quoted = False
    escaped = False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
            if depth > MAX_NESTING:
                raise ToolCallParseError("Tool call nesting is too deep.")
            if depth == 1:
                start = index
        elif char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                yield start, index + 1
                start = -1


def _normalize(value: object) -> tuple[str, dict[str, object]]:
    if not isinstance(value, dict):
        raise ToolCallParseError("Tool call must be a JSON object.")
    name = value.get("tool_name") or value.get("tool")
    if isinstance(name, dict) and value.get("type") == "function":
        name = value.get("name")
    if not isinstance(name, str) or _TOOL_NAME.fullmatch(name.strip()) is None:
        raise ToolCallParseError("Tool call has no valid tool_name.")
    args = value.get("tool_args", value.get("args", value.get("arguments")))
    if args is None and "parameters" in value:
        args = value["parameters"]
    if not isinstance(args, dict):
        raise ToolCallParseError("Tool call has no object tool_args.")
    if any(not isinstance(key, str) for key in args):
        raise ToolCallParseError("Tool argument names must be text.")
    return name.strip(), dict(args)


def _try(text: str) -> tuple[str, dict[str, object]] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    try:
        return _normalize(parsed)
    except ToolCallParseError:
        return None


def _recover(text: str) -> tuple[str, dict[str, object]] | None:
    """Opt-in recovery of a near-miss JSON object via the vendored parser."""
    from bq.dirty_json import DirtyJson

    try:
        parsed = DirtyJson.parse_string(text)  # type: ignore[no-untyped-call]
    except Exception:
        return None
    try:
        return _normalize(parsed)
    except ToolCallParseError:
        return None


def parse_tool_call(
    response: str, *, recover: bool = False
) -> tuple[str, dict[str, object]]:
    """Return ``(tool_name, tool_args)`` or raise ``ToolCallParseError``.

    ``recover=True`` enables the vendored dirty-JSON parser as a last resort for
    slightly malformed calls (single quotes, trailing commas, comments). It stays
    off by default so a strict caller never silently accepts a repaired call.
    """
    if not isinstance(response, str) or not response.strip():
        raise ToolCallParseError("Empty model response.")
    text = response.strip()
    if len(text) > MAX_SCAN_CHARS:
        raise ToolCallParseError("Model response is too large to parse.")

    whole = _try(text)
    if whole is not None:
        return whole

    for match in _FENCE.finditer(text):
        fenced = _try(match.group(1).strip())
        if fenced is not None:
            return fenced

    for start, end in _object_spans(text):
        candidate = _try(text[start:end])
        if candidate is not None:
            return candidate

    if recover:
        recovered = _recover(text)
        if recovered is not None:
            return recovered
        for match in _FENCE.finditer(text):
            recovered = _recover(match.group(1).strip())
            if recovered is not None:
                return recovered

    raise ToolCallParseError("No tool call found in the model response.")


def looks_like_tool_call(response: str) -> bool:
    try:
        parse_tool_call(response)
    except ToolCallParseError:
        return False
    return True
