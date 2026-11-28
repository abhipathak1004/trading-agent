import json
from pathlib import Path
from typing import Any

import pytest

from bq.decision.types import Answer, DecisionOutcome, JudgmentSet, QuestionType
from bq.infra.database import migrate
from bq.llm_client import DEFAULT_TIMEOUT_SECONDS, LLMClientError
from bq.llm_result import LLMResult, LLMUsage
from bq.llm_service import LLMService
from bq.logbook import LoggingConfig, configure_logbook, reset_logbook
from bq.model_router import ModelRouter
from bq.settings import Provider, SettingsStore

PASSWORD_KEY = "sk-test-1234567890abcdef"


class FakeClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    def complete(
        self,
        *,
        model: str,
        api_key: str,
        system: str,
        prompt: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> LLMResult:
        del model, api_key, system, prompt, timeout
        if self.error is not None:
            raise self.error
        return LLMResult(
            text="the answer",
            reasoning="because",
            usage=LLMUsage(input_tokens=5, output_tokens=7),
            finish_reason="STOP",
        )


def _store(tmp_path: Path) -> SettingsStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = SettingsStore(path)
    store.save_config(
        provider=Provider.GEMINI,
        model_name="gemini-2.5-pro",
        news_feed_url="https://example.com/feed.xml",
        news_memory_ttl_days=30,
    )
    store.set_api_key(Provider.GEMINI, PASSWORD_KEY)
    return store


def _entries(tmp_path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted((tmp_path / "logs").glob("bq-*.jsonl")):
        entries.extend(json.loads(line) for line in path.read_text().splitlines())
    return entries


def test_llm_service_logs_a_successful_call_without_the_key(tmp_path: Path) -> None:
    book = configure_logbook(tmp_path, LoggingConfig())
    try:
        service = LLMService(_store(tmp_path), client=FakeClient())
        outcome = service.complete(
            prompt="hello",
            system="be terse",
            task_id="analysis.judge",
        )
        assert outcome.text == "the answer"
    finally:
        reset_logbook()
    book.flush()

    entries = _entries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "llm_call"
    data = entry["data"]
    assert data["provider"] == "GEMINI"
    assert data["model"] == "gemini-2.5-pro"
    assert data["task_id"] == "analysis.judge"
    assert data["prompt"] == "hello"
    assert data["system"] == "be terse"
    assert data["response"] == "the answer"
    assert data["reasoning"] == "because"
    assert data["input_tokens"] == 5
    assert data["output_tokens"] == 7
    assert data["finish_reason"] == "STOP"
    assert data["ok"] is True
    assert PASSWORD_KEY not in (tmp_path / "logs" / book.files()[0].name).read_text()


def test_llm_service_logs_a_failed_call_and_reraises(tmp_path: Path) -> None:
    book = configure_logbook(tmp_path, LoggingConfig())
    try:
        service = LLMService(
            _store(tmp_path), client=FakeClient(error=LLMClientError("down"))
        )
        with pytest.raises(LLMClientError):
            service.complete(prompt="hello", task_id="analysis.judge")
    finally:
        reset_logbook()
    book.flush()

    data = _entries(tmp_path)[0]["data"]
    assert data["ok"] is False
    assert data["error"] == "LLMClientError"
    assert data["prompt"] == "hello"
    assert PASSWORD_KEY not in (tmp_path / "logs" / book.files()[0].name).read_text()


def test_model_router_decision_is_logged(tmp_path: Path) -> None:
    book = configure_logbook(tmp_path, LoggingConfig())
    try:
        router = ModelRouter(_store(tmp_path))
        outcome = DecisionOutcome(
            judgments=JudgmentSet(
                battery="final_gate",
                answers=(("action", Answer(kind=QuestionType.CHOICE, choice="BUY")),),
                model="gemini-2.5-pro",
                source="llm_fallback",
                latency_ms=12.5,
            ),
            provider="GEMINI",
            model="gemini-2.5-pro",
            tier="flash",
        )
        router._record_decision("final_decision_gate", "analysis.final_gate", outcome)
    finally:
        reset_logbook()
    book.flush()

    entry = _entries(tmp_path)[0]
    assert entry["kind"] == "decision"
    data = entry["data"]
    assert data["scope"] == "model_decision"
    assert data["battery"] == "final_gate"
    assert data["source"] == "llm_fallback"
    assert data["agent_id"] == "final_decision_gate"
    assert data["answers"]["action"]["choice"] == "BUY"
