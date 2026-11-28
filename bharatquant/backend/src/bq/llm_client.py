"""Real model provider clients.

The first modules that actually talk to a model provider. Kept small and
defensive:

* HTTPS only; the API key travels in a header (Gemini) or the Authorization
  header (OpenAI-compatible), never in a URL that could end up in a log.
* Bounded: a hard timeout, a response-size cap, and no streaming. One call,
  one bounded result back.
* Transient failures (timeouts, 429, 5xx) retry with capped exponential backoff
  using ``bq.llm_retry`` (adapted from Agent Zero). 4xx errors the caller must
  fix do not retry.
* Errors are typed and secret-free. The caller gets ``LLMClientError``, never a
  raw provider traceback that could embed request details.
* Transport is injectable so tests never touch the network.

Each client returns a ``bq.llm_result.LLMResult``: text, separated reasoning,
usage, finish reason and normalised tool calls. Reasoning parsing and tool-call
extraction live in ``bq.llm_extract`` (also adapted from Agent Zero).

Two wire formats cover most providers: ``GeminiClient`` (Google Generative
Language REST) and ``OpenAICompatClient`` (chat completions, fitting DeepSeek,
OpenAI and a custom OpenAI-compatible endpoint). ``BedrockClient`` speaks AWS
Bedrock's model-neutral ``Converse`` format, which is a third shape.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from bq.llm_extract import (
    extract_gemini_calls,
    extract_tool_calls,
    map_finish_reason,
    split_thinking,
)
from bq.llm_result import FunctionCall, LLMResult, parse_usage
from bq.llm_retry import run_with_retry
from bq.settings import Provider

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60.0
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"
# Bedrock is regional and names the model in the path; the region is
# configured and the model id arrives as the call's `model`.
BEDROCK_ENDPOINT = (
    "https://bedrock-runtime.{region}.amazonaws.com/model/{model}/converse"
)
BEDROCK_DEFAULT_REGION = "us-east-1"
_USER_AGENT = "BharatQuant/0.1 (personal research)"
MAX_TOOL_RESULT_CHARS = 24_000


class LLMClientError(ValueError):
    """A provider call failed or returned something unusable."""


class LLMNotConfigured(LLMClientError):
    """No provider key / model name is configured."""


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """A tool call the model asked for, replayed to it on the next turn.

    Providers with native tool use require the assistant's request and its
    matching result to appear in the conversation. Without this the model has
    no record of having called the tool and simply asks for it again.
    """

    call_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """One tool's output, returned to the model."""

    call_id: str
    name: str
    content: str

    def __post_init__(self) -> None:
        if len(self.content) > MAX_TOOL_RESULT_CHARS:
            object.__setattr__(self, "content", self.content[:MAX_TOOL_RESULT_CHARS])


class ModelClient(Protocol):
    def complete(
        self,
        *,
        model: str,
        api_key: str,
        system: str,
        prompt: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        tools: Sequence[Mapping[str, object]] | None = None,
        tool_call: ToolCallRequest | None = None,
        tool_result: ToolResult | None = None,
    ) -> LLMResult: ...


def _as_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LLMClientError(f"Provider returned an invalid {label}.")
    return value


def _post(client: httpx.Client, url: str, **kwargs: object) -> dict[str, object]:
    """POST and return a bounded JSON object, mapping errors to typed values."""
    try:
        response = client.post(url, **kwargs)  # type: ignore[arg-type]
    except httpx.HTTPError as exc:
        raise LLMClientError(f"Provider request failed: {type(exc).__name__}.") from exc
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise LLMClientError("Provider response exceeded the size limit.")
    if response.status_code >= 400:
        # Do not echo the body: it can contain account or key fragments.
        raise LLMClientError(f"Provider returned HTTP {response.status_code}.")
    try:
        return _as_dict(response.json(), "response body")
    except ValueError as exc:
        raise LLMClientError("Provider response was not valid JSON.") from exc


def _call_with_retry(
    *,
    transport: httpx.BaseTransport | None,
    timeout: float,
    url: str,
    body: dict[str, object],
    headers: dict[str, str],
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            return _post(client, url, json=body, headers=headers)

    return run_with_retry(operation)  # type: ignore[return-value]


def _to_function_calls(raw_calls: list[dict[str, object]]) -> tuple[FunctionCall, ...]:
    calls: list[FunctionCall] = []
    for item in raw_calls:
        name = item.get("name")
        arguments = item.get("arguments")
        if isinstance(name, str) and name and isinstance(arguments, dict):
            calls.append(
                FunctionCall(
                    name=name,
                    arguments=arguments,
                    call_id=str(item.get("call_id") or ""),
                )
            )
    return tuple(calls)


class GeminiClient:
    """Google Generative Language ``generateContent`` client."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def complete(
        self,
        *,
        model: str,
        api_key: str,
        system: str,
        prompt: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        tools: Sequence[Mapping[str, object]] | None = None,
        tool_call: ToolCallRequest | None = None,
        tool_result: ToolResult | None = None,
    ) -> LLMResult:
        contents: list[dict[str, object]] = []
        # Gemini names the tool round trip by function name, in this order:
        # model turns carry `functionCall` parts, then a user turn carries the
        # matching `functionResponse`.
        if tool_call is not None:
            contents.append(
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": tool_call.name,
                                "args": dict(tool_call.arguments),
                            }
                        }
                    ],
                }
            )
        if tool_result is not None:
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": tool_result.name,
                                "response": {"result": tool_result.content},
                            }
                        }
                    ],
                }
            )
        contents.append({"role": "user", "parts": [{"text": prompt}]})
        body: dict[str, object] = {
            "contents": contents,
            "generationConfig": {"temperature": 0.2},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters", {"type": "object"}),
                        }
                        for tool in tools
                        if isinstance(tool.get("name"), str)
                    ]
                }
            ]
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        payload = _call_with_retry(
            transport=self.transport,
            timeout=timeout,
            url=GEMINI_ENDPOINT.format(model=model),
            body=body,
            headers=headers,
        )
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise LLMClientError("Provider returned no candidates.")
        first = _as_dict(candidates[0], "candidate")
        content = _as_dict(first.get("content"), "content")
        parts = content.get("parts")
        text = ""
        if isinstance(parts, list):
            text = "".join(
                str(part.get("text", ""))
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        visible, reasoning = split_thinking(text)
        tool_calls = _to_function_calls(extract_gemini_calls(candidates))
        if not visible and not tool_calls:
            raise LLMClientError("Provider returned empty text.")
        return LLMResult(
            text=visible,
            reasoning=reasoning,
            usage=parse_usage(payload.get("usageMetadata")),
            finish_reason=map_finish_reason(first.get("finishReason")),
            function_calls=tool_calls,
            raw=payload,
        )


class OpenAICompatClient:
    """OpenAI-compatible chat client (DeepSeek, OpenAI, or a custom endpoint)."""

    def __init__(
        self,
        endpoint: str = DEEPSEEK_ENDPOINT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parts = urlsplit(endpoint)
        loopback_http = parts.scheme == "http" and parts.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if parts.scheme != "https" and not loopback_http:
            raise LLMClientError(
                "Chat endpoint must be HTTPS, or HTTP only on a loopback host."
            )
        if not parts.hostname:
            raise LLMClientError("Chat endpoint must name a host.")
        # Accept either an OpenAI-compatible base (``.../v1``) or the full
        # completions URL. Without this, a loopback endpoint configured the way
        # every OpenAI SDK documents POSTs to ``/v1`` and gets a 404.
        self.endpoint = (
            endpoint
            if endpoint.rstrip("/").endswith("/chat/completions")
            else f"{endpoint.rstrip('/')}/chat/completions"
        )
        # A loopback endpoint is a local model server (e.g. Ollama). Ask it not
        # to generate chain-of-thought: reasoning burns the token budget before
        # any answer and dilutes strict JSON. Hosted providers keep their default.
        self._loopback = loopback_http
        self.transport = transport

    def complete(
        self,
        *,
        model: str,
        api_key: str,
        system: str,
        prompt: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        tools: Sequence[Mapping[str, object]] | None = None,
        tool_call: ToolCallRequest | None = None,
        tool_result: ToolResult | None = None,
    ) -> LLMResult:
        messages: list[dict[str, object]] = []
        if system:
            messages.append({"role": "system", "content": system})
        # Replay the native exchange first: an assistant `tool_calls` message,
        # then the `tool` message carrying its result. OpenAI-compatible APIs
        # require both, keyed by the same id.
        if tool_call is not None:
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call.call_id,
                            "type": "function",
                            "function": {
                                "name": tool_call.name,
                                "arguments": json.dumps(dict(tool_call.arguments)),
                            },
                        }
                    ],
                }
            )
        if tool_result is not None:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_result.call_id,
                    "content": tool_result.content,
                }
            )
        messages.append({"role": "user", "content": prompt})
        body: dict[str, object] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
        }
        if self._loopback:
            body["reasoning_effort"] = "none"
        if tools:
            body["tools"] = [
                {"type": "function", "function": dict(tool)} for tool in tools
            ]
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        payload = _call_with_retry(
            transport=self.transport,
            timeout=timeout,
            url=self.endpoint,
            body=body,
            headers=headers,
        )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMClientError("Provider returned no choices.")
        choice = _as_dict(choices[0], "choice")
        message = _as_dict(choice.get("message"), "message")
        content = message.get("content")
        raw_text = content if isinstance(content, str) else ""
        visible, reasoning = split_thinking(raw_text)
        native_reasoning = message.get("reasoning_content")
        if isinstance(native_reasoning, str) and native_reasoning.strip():
            reasoning = (
                f"{native_reasoning.strip()}\n\n{reasoning}".strip()
                if reasoning
                else native_reasoning.strip()
            )
        tool_calls = _to_function_calls(extract_tool_calls(message))
        if not visible and not tool_calls:
            raise LLMClientError("Provider returned empty text.")
        return LLMResult(
            text=visible,
            reasoning=reasoning,
            usage=parse_usage(payload.get("usage")),
            finish_reason=map_finish_reason(choice.get("finish_reason")),
            function_calls=tool_calls,
            raw=payload,
        )


class BedrockClient:
    """Amazon Bedrock ``Converse`` client, authenticated with a bearer token.

    Bedrock API keys are bearer tokens, not AWS secret keys, so no SigV4
    signing happens here (ADR-051). ``Converse`` is Bedrock's model-neutral
    request format, which is why one client serves every vendor's model that
    Bedrock hosts.

    Two properties of the real API shape the code:

    * The model id lives in the **path**, so an id with anything other than
      ``[A-Za-z0-9._:-]`` is refused rather than path-escaped — a slash there
      would silently address a different resource.
    * A short-term key only works in the region it was minted in, so the region
      is configuration, not a guess.
    """

    _MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")

    def __init__(
        self,
        region: str = BEDROCK_DEFAULT_REGION,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.region = _validate_bedrock_region(region)
        self.transport = transport

    def complete(
        self,
        *,
        model: str,
        api_key: str,
        system: str,
        prompt: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        tools: Sequence[Mapping[str, object]] | None = None,
        tool_call: ToolCallRequest | None = None,
        tool_result: ToolResult | None = None,
    ) -> LLMResult:
        if self._MODEL_ID.fullmatch(model) is None:
            raise LLMClientError(
                "Bedrock model id may contain only letters, digits, '.', ':' or '-'."
            )
        messages: list[dict[str, object]] = []
        if tool_call is not None:
            # Converse requires the assistant's toolUse block, then a *user*
            # message carrying the matching toolResult. Sending only the result
            # leaves the model with no record of its own call, so it asks for
            # the same tool again on the next turn.
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": tool_call.call_id,
                                "name": tool_call.name,
                                "input": dict(tool_call.arguments),
                            }
                        }
                    ],
                }
            )
        if tool_result is not None:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": tool_result.call_id,
                                "content": [{"text": tool_result.content}],
                            }
                        }
                    ],
                }
            )
        messages.append({"role": "user", "content": [{"text": prompt}]})

        body: dict[str, object] = {
            "messages": messages,
            "inferenceConfig": {"temperature": 0.2},
        }
        if system:
            body["system"] = [{"text": system}]
        tool_config = _bedrock_tool_config(tools)
        if tool_config is not None:
            body["toolConfig"] = tool_config
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        payload = _call_with_retry(
            transport=self.transport,
            timeout=timeout,
            url=BEDROCK_ENDPOINT.format(region=self.region, model=model),
            body=body,
            headers=headers,
        )
        output = _as_dict(payload.get("output"), "output")
        message = _as_dict(output.get("message"), "message")
        content = message.get("content")
        text = ""
        tool_uses: list[dict[str, object]] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("text"), str):
                    text += str(block["text"])
                use = block.get("toolUse")
                if isinstance(use, dict):
                    tool_uses.append(
                        {
                            "name": use.get("name"),
                            "arguments": use.get("input"),
                            "call_id": use.get("toolUseId"),
                        }
                    )
        visible, reasoning = split_thinking(text)
        calls = _to_function_calls(tool_uses)
        # A tool-only answer carries no text and is a legitimate reply.
        if not visible and not calls:
            raise LLMClientError("Provider returned empty text.")
        return LLMResult(
            text=visible,
            reasoning=reasoning,
            usage=parse_usage(payload.get("usage")),
            finish_reason=map_finish_reason(payload.get("stopReason")),
            function_calls=calls,
            raw=payload,
        )


def _bedrock_tool_config(
    tools: Sequence[Mapping[str, object]] | None,
) -> dict[str, object] | None:
    """Translate provider-neutral tool schemas into ``Converse``'s shape.

    ``Converse`` nests the JSON Schema one level deeper than OpenAI
    (``toolSpec.inputSchema.json``) and rejects an empty tool list, so ``None``
    is returned rather than an empty configuration.
    """
    if not tools:
        return None
    specs: list[dict[str, object]] = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        parameters = tool.get("parameters")
        spec: dict[str, object] = {
            "name": name,
            "inputSchema": {
                "json": parameters
                if isinstance(parameters, dict)
                else {"type": "object"}
            },
        }
        description = tool.get("description")
        if isinstance(description, str) and description:
            spec["description"] = description[:1_000]
        specs.append({"toolSpec": spec})
    return {"tools": specs} if specs else None


def _validate_bedrock_region(value: str) -> str:
    """A bare AWS region name, e.g. ``us-east-1``.

    Validated rather than only stored, because it is interpolated into a
    hostname: accepting an arbitrary string would let configuration point the
    request at a host of its choosing.
    """
    region = (value or "").strip()
    if not region:
        return BEDROCK_DEFAULT_REGION
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}", region):
        raise LLMClientError(f"{region!r} is not an AWS region name.")
    return region


def client_for(
    provider: Provider,
    *,
    transport: httpx.BaseTransport | None = None,
    custom_endpoint: str | None = None,
    bedrock_region: str | None = None,
) -> ModelClient:
    """Pick the wire format for a configured provider."""
    if provider is Provider.GEMINI:
        return GeminiClient(transport=transport)
    if provider is Provider.DEEPSEEK:
        return OpenAICompatClient(transport=transport)
    if provider is Provider.OPENAI:
        return OpenAICompatClient(OPENAI_ENDPOINT, transport=transport)
    if provider is Provider.BEDROCK:
        return BedrockClient(
            region=bedrock_region or BEDROCK_DEFAULT_REGION, transport=transport
        )
    if provider is Provider.TYPESAFE_JEV:
        raise LLMNotConfigured(
            "TypeSafe Jev is a decision provider; use the decision router."
        )
    # CUSTOM defaults to an OpenAI-compatible wire format (e.g. a local model).
    if custom_endpoint:
        return OpenAICompatClient(custom_endpoint, transport=transport)
    return OpenAICompatClient(transport=transport)


# Backwards-compatible alias: earlier code imported ``DeepSeekClient``.
DeepSeekClient = OpenAICompatClient
