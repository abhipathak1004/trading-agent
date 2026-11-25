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

Two wire formats cover the configured providers: ``GeminiClient`` (Google
Generative Language REST) and ``OpenAICompatClient`` (chat completions, fitting
DeepSeek, OpenAI and a custom OpenAI-compatible endpoint).
"""

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
_USER_AGENT = "BharatQuant/0.1 (personal research)"


class LLMClientError(ValueError):
    """A provider call failed or returned something unusable."""


class LLMNotConfigured(LLMClientError):
    """No provider key / model name is configured."""


class ModelClient(Protocol):
    def complete(
        self,
        *,
        model: str,
        api_key: str,
        system: str,
        prompt: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
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
    ) -> LLMResult:
        body: dict[str, object] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
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
        if parts.scheme != "https" or not parts.hostname:
            raise LLMClientError("Chat endpoint must be an HTTPS URL.")
        self.endpoint = endpoint
        self.transport = transport

    def complete(
        self,
        *,
        model: str,
        api_key: str,
        system: str,
        prompt: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> LLMResult:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, object] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
        }
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


def client_for(
    provider: Provider, *, transport: httpx.BaseTransport | None = None
) -> ModelClient:
    """Pick the wire format for a configured provider."""
    if provider is Provider.GEMINI:
        return GeminiClient(transport=transport)
    if provider is Provider.DEEPSEEK:
        return OpenAICompatClient(transport=transport)
    if provider is Provider.OPENAI:
        return OpenAICompatClient(OPENAI_ENDPOINT, transport=transport)
    # CUSTOM defaults to an OpenAI-compatible wire format.
    return OpenAICompatClient(transport=transport)


# Backwards-compatible alias: earlier code imported ``DeepSeekClient``.
DeepSeekClient = OpenAICompatClient
