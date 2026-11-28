from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from bq.infra.database import migrate
from bq.llm_client import (
    DeepSeekClient,
    GeminiClient,
    LLMClientError,
    LLMNotConfigured,
    client_for,
)
from bq.llm_service import LLMService, candidate_for
from bq.settings import Provider, SettingsStore

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _transport(handler: object) -> httpx.MockTransport:
    assert callable(handler)
    return httpx.MockTransport(handler)


def test_gemini_client_parses_text_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "key-123"
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3},
            },
        )

    client = GeminiClient(transport=_transport(handler))
    result = client.complete(
        model="gemini-2.5-pro", api_key="key-123", system="s", prompt="p"
    )
    assert result.text == "hello"
    assert result.input_tokens == 10
    assert result.output_tokens == 3


def test_gemini_client_rejects_http_error_without_leaking_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "secret-ish detail"})

    client = GeminiClient(transport=_transport(handler))
    with pytest.raises(LLMClientError, match="HTTP 403"):
        client.complete(model="m", api_key="k", system="", prompt="p")


def test_deepseek_client_parses_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    client = DeepSeekClient(transport=_transport(handler))
    result = client.complete(
        model="deepseek-chat", api_key="sk-test", system="", prompt="p"
    )
    assert result.text == "answer"
    assert result.input_tokens == 5


def test_deepseek_client_requires_https_endpoint() -> None:
    with pytest.raises(LLMClientError):
        DeepSeekClient(endpoint="http://insecure.example/chat")


def test_client_for_maps_providers() -> None:
    assert isinstance(client_for(Provider.GEMINI), GeminiClient)
    assert isinstance(client_for(Provider.DEEPSEEK), DeepSeekClient)
    assert isinstance(client_for(Provider.OPENAI), DeepSeekClient)


def _store(tmp_path: Path) -> SettingsStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return SettingsStore(path, FrozenClock())


def test_candidate_requires_model_and_matching_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(LLMNotConfigured):
        candidate_for(store.load())
    store.save_config(
        provider=Provider.GEMINI,
        model_name="gemini-2.5-pro",
        news_feed_url="https://example.com/feed.xml",
        news_memory_ttl_days=30,
    )
    with pytest.raises(LLMNotConfigured):
        candidate_for(store.load())
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-1234567890")
    candidate = candidate_for(store.load())
    assert candidate.model_id == "gemini-2.5-pro"
    assert candidate.available is True


def test_service_completes_with_injected_client(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_config(
        provider=Provider.GEMINI,
        model_name="gemini-2.5-pro",
        news_feed_url="https://example.com/feed.xml",
        news_memory_ttl_days=30,
    )
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-1234567890")

    class FakeClient:
        def complete(
            self,
            *,
            model: str,
            api_key: str,
            system: str,
            prompt: str,
            timeout: float,
        ) -> object:
            from bq.llm_result import LLMResult as ModelResult
            from bq.llm_result import LLMUsage

            assert api_key == "AIza-synthetic-1234567890"
            return ModelResult(
                text="ok", usage=LLMUsage(input_tokens=1, output_tokens=1)
            )

    from typing import cast

    from bq.llm_client import ModelClient

    service = LLMService(store, client=cast(ModelClient, FakeClient()))
    result = service.complete(prompt="hello")
    assert result.text == "ok"
    assert result.provider is Provider.GEMINI


def test_service_raises_not_configured_without_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(LLMNotConfigured):
        LLMService(store, client=object()).complete(prompt="x")  # type: ignore[arg-type]


def test_llm_endpoint_runs_with_configured_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import httpx as _httpx

    from bq.api import ALLOWED_ORIGIN, create_app
    from bq.auth import OwnerAuth
    from bq.llm_result import LLMResult as ModelResult
    from bq.llm_result import LLMUsage

    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = SettingsStore(path, FrozenClock())
    store.save_config(
        provider=Provider.GEMINI,
        model_name="gemini-2.5-pro",
        news_feed_url="https://example.com/feed.xml",
        news_memory_ttl_days=30,
    )
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-1234567890")

    # Replace the network client with a fake so the test never calls out.
    import bq.llm_service as service_module

    class FakeClient:
        def complete(self, **_kwargs: object) -> ModelResult:
            return ModelResult(
                text="live-ok", usage=LLMUsage(input_tokens=4, output_tokens=2)
            )

    monkeypatch.setattr(service_module, "client_for", lambda _provider: FakeClient())

    auth = OwnerAuth(path)
    app = create_app(database_path=path)
    password = "synthetic owner password"

    async def check() -> None:
        async with _httpx.AsyncClient(
            transport=_httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            assert (
                await client.post("/api/v1/llm/complete", json={"prompt": "x"})
            ).status_code == 401
            auth.setup(password)
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": password},
            )
            csrf = login.json()["csrf_token"]

            missing_csrf = await client.post(
                "/api/v1/llm/complete",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"prompt": "hello"},
            )
            assert missing_csrf.status_code == 403

            ok = await client.post(
                "/api/v1/llm/complete",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json={"prompt": "hello"},
            )
            assert ok.status_code == 200
            body = ok.json()
            assert body["text"] == "live-ok"
            assert body["provider"] == "GEMINI"
            assert body["model"] == "gemini-2.5-pro"

    asyncio.run(check())
