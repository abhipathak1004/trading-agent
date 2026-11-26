import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from bq.logbook import (
    MAX_TEXT_BYTES,
    TRUNCATION_MARKER,
    Logbook,
    LoggingConfig,
    LoggingConfigError,
    configure_logbook,
    get_logbook,
    record_decision,
    record_llm_call,
    reset_logbook,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _book(tmp_path: Path, **overrides: object) -> Logbook:
    return Logbook(
        tmp_path,
        LoggingConfig(**overrides),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
    )


def _payloads(
    tmp_path: Path, name: str = "bq-2026-11-26.jsonl"
) -> list[dict[str, Any]]:
    lines = (tmp_path / "logs" / name).read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_record_waits_in_pending_until_flush(tmp_path: Path) -> None:
    book = _book(tmp_path)
    book.record("system", message="hello", count=1)

    assert book.pending_bytes() > 0
    assert not list((tmp_path / "logs").glob("bq-*.jsonl"))

    result = book.flush()

    assert result.written == 1
    assert result.files == ("bq-2026-11-26.jsonl",)
    assert book.pending_bytes() == 0
    payload = _payloads(tmp_path)[0]
    assert payload["kind"] == "system"
    assert payload["level"] == "info"
    assert payload["data"] == {"count": 1, "message": "hello"}


def test_flush_is_idempotent(tmp_path: Path) -> None:
    book = _book(tmp_path)
    book.record("system", message="once")
    assert book.flush().written == 1
    second = book.flush()
    assert second.written == 0
    assert len(_payloads(tmp_path)) == 1


def test_retention_prunes_files_older_than_the_window(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    old = logs / "bq-2026-11-01.jsonl"
    old.write_text('{"v":1,"ts":"2026-11-01T00:00:00Z","kind":"system","data":{}}\n')
    fresh = logs / "bq-2026-11-25.jsonl"
    fresh.write_text('{"v":1,"ts":"2026-11-25T00:00:00Z","kind":"system","data":{}}\n')

    pruned = _book(tmp_path, retention_days=14).prune()

    assert pruned == ("bq-2026-11-01.jsonl",)
    assert old.exists() is False
    assert fresh.exists() is True


def test_long_text_is_truncated_and_flagged(tmp_path: Path) -> None:
    book = _book(tmp_path)
    book.record("llm_call", prompt="x" * (MAX_TEXT_BYTES + 100))
    book.flush()

    payload = _payloads(tmp_path)[0]
    assert payload["truncated"] is True
    assert payload["data"]["prompt"].endswith(TRUNCATION_MARKER)
    assert len(payload["data"]["prompt"].encode("utf-8")) <= MAX_TEXT_BYTES + len(
        TRUNCATION_MARKER.encode("utf-8")
    )


def test_secrets_are_redacted(tmp_path: Path) -> None:
    book = _book(tmp_path)
    book.record(
        "llm_call",
        prompt=(
            "Authorization: Bearer abcdefghijklmnop "
            "and sk-1234567890abcdef and x-goog-api-key: upper-secret"
        ),
    )
    book.flush()

    text = (tmp_path / "logs" / "bq-2026-11-26.jsonl").read_text()
    assert "abcdefghijklmnop" not in text
    assert "1234567890abcdef" not in text
    assert "upper-secret" not in text
    assert "[redacted]" in text


def test_disabled_logbook_writes_nothing(tmp_path: Path) -> None:
    book = _book(tmp_path, enabled=False)
    book.record("system", message="ignored")

    assert not (tmp_path / "logs").exists()
    assert book.pending_bytes() == 0
    assert book.flush().written == 0


def test_two_instances_do_not_lose_or_duplicate_entries(tmp_path: Path) -> None:
    first = _book(tmp_path)
    second = _book(tmp_path)
    first.record("system", n=1)
    second.record("system", n=2)

    result = first.flush()

    assert result.written == 2
    assert second.flush().written == 0
    payloads = _payloads(tmp_path)
    assert sorted(item["data"]["n"] for item in payloads) == [1, 2]


def test_status_and_recent_are_bounded_reads(tmp_path: Path) -> None:
    book = _book(tmp_path)
    book.record("system", message="one")
    book.record("system", message="two")
    book.flush()

    status = book.status()
    assert status["directory"] == str(tmp_path / "logs")
    assert status["enabled"] is True
    assert status["retention_days"] == 14
    assert status["flush_minutes"] == 15
    files = status["files"]
    assert isinstance(files, list) and files[0]["name"] == "bq-2026-11-26.jsonl"
    recent = book.recent(limit=1)
    assert len(recent) == 1


def test_logs_flush_job_handler(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from bq.infra.database import migrate
    from bq.infra.job_handlers import LOGS_FLUSH_JOB, build_job_handlers
    from bq.settings import SettingsStore

    path = tmp_path / "bq.db"
    migrate(path, create=True)
    reset_logbook()
    configure_logbook(tmp_path, LoggingConfig()).record("system", message="job")

    handlers = build_job_handlers(path, SettingsStore(path))
    result = cast(
        dict[str, object],
        handlers[LOGS_FLUSH_JOB](SimpleNamespace()),  # type: ignore[arg-type]
    )

    assert result["written"] == 1
    assert list((tmp_path / "logs").glob("bq-*.jsonl"))
    reset_logbook()


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(LoggingConfigError):
        LoggingConfig(retention_days=0)
    with pytest.raises(LoggingConfigError):
        LoggingConfig(retention_days=3651)
    with pytest.raises(LoggingConfigError):
        LoggingConfig(flush_minutes=0)
    with pytest.raises(LoggingConfigError):
        LoggingConfig(flush_minutes=1441)


def test_module_helpers_configure_and_noop_when_unset(tmp_path: Path) -> None:
    reset_logbook()
    record_llm_call(
        provider="GEMINI",
        model="gemini-2.5-pro",
        task_id="analysis.judge",
        latency_ms=12.5,
        ok=True,
        prompt="prompt",
        system="system",
        response="answer",
        input_tokens=3,
        output_tokens=4,
    )
    record_decision("final_gate", symbol="NSE:LT", action="BUY")

    book = configure_logbook(tmp_path, LoggingConfig())
    assert get_logbook() is book
    record_llm_call(
        provider="GEMINI",
        model="gemini-2.5-pro",
        task_id="analysis.judge",
        latency_ms=12.5,
        ok=True,
        prompt="prompt",
        system="system",
        response="answer",
        input_tokens=3,
        output_tokens=4,
        agent_id="research_judge",
    )
    record_decision("final_gate", symbol="NSE:LT", action="BUY")
    book.flush()

    payloads = _payloads(tmp_path)
    assert [item["kind"] for item in payloads] == ["llm_call", "decision"]
    call = payloads[0]["data"]
    assert call["provider"] == "GEMINI"
    assert call["model"] == "gemini-2.5-pro"
    assert call["prompt"] == "prompt"
    assert call["response"] == "answer"
    assert call["input_tokens"] == 3
    assert call["output_tokens"] == 4
    assert call["agent_id"] == "research_judge"
    assert payloads[1]["data"] == {
        "scope": "final_gate",
        "symbol": "NSE:LT",
        "action": "BUY",
    }
    reset_logbook()
