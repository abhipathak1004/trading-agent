"""The LLM provider protocol, and the parse helpers every agent shares.

Vendored from `hedge_fund/llm/client.py`, reduced to the provider-agnostic half:
the `LLMClient` protocol, the two error types the agent layer branches on, the
JSON extraction helper, and the prompt-cache identity function. Every concrete
transport (Anthropic/OpenAI/DeepSeek/Google/xAI/Kimi langchain clients, the
TypeSafe Jev adapter, and the `make_llm` factory that chose between them) was
dropped: BharatQuant supplies its own `LLMClient` implementation over
`ModelRouter`, so no langchain package is needed and no provider id is
hardcoded here. See `bq/aihf/bridge/llm.py`.

`DEFAULT_MODEL` is kept only as a sentinel for a fund that staffed no LLM
agent; it is never called.
"""

from __future__ import annotations

import json
import re
from typing import Protocol, runtime_checkable

# A record-only marker for a mandate that staffs no LLM persona. BharatQuant
# resolves every real model through Settings; nothing routes an id from here.
DEFAULT_MODEL = "unconfigured"


class LLMParseError(ValueError):
    """The model's response did not contain parseable JSON."""


class LLMCallError(RuntimeError):
    """Provider failure with optional, credential-free persistence context."""

    def __init__(self, message: str, diagnostic_record: dict | None = None) -> None:
        super().__init__(message)
        self.diagnostic_record = diagnostic_record


@runtime_checkable
class LLMClient(Protocol):
    """Protocol all LLM providers must satisfy.

    complete() returns text (native text or an adapter's compatibility JSON).
    Providers may optionally implement cache_key(agent, system, user) when
    their request contains additional semantics beyond the two prompts.
    Providers should raise on transport failure — the LLMAgent layer decides
    to abstain, not the provider.
    """

    model: str

    def complete(self, system: str, user: str) -> str: ...


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM response.

    Tries: ```json fence -> whole string -> first balanced {...} block.
    Raises LLMParseError if nothing parses.
    """
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    raise LLMParseError(f"no JSON object found in response: {text[:200]!r}")
