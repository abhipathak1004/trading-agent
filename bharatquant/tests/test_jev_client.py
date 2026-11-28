import json

import httpx
import pytest

from bq.decision.battery import SCREENER, questions_for
from bq.decision.client import (
    JevClient,
    JevError,
    JevUnavailable,
)


def _transport(handler: object) -> httpx.MockTransport:
    assert callable(handler)
    return httpx.MockTransport(handler)


def _response(
    *, model: str = "jev-1.14.0", request_id: str = "req-1"
) -> dict[str, object]:
    return {
        "model": model,
        "request_id": request_id,
        "answers": {
            "setup_direction": {
                "type": "choice",
                "choice": "long",
                "probabilities": {"long": 0.8, "short": 0.1, "stand_aside": 0.1},
                "confidence": 0.8,
            },
            "setup_quality": {
                "type": "score",
                "score": 3.0,
                "probabilities": {"0": 0.0, "1": 0.1, "2": 0.2, "3": 0.6, "4": 0.1},
                "confidence": 0.7,
            },
            "worth_deep_research": {"type": "noul", "noul": 0.82},
            "event_risk": {"type": "noul", "noul": 0.12},
        },
        "usage": {"input_tokens": 120, "output_tokens": 40},
    }


def test_valid_response_is_typed_and_provenanced() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer jev-key"
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_response())

    client = JevClient(transport=_transport(handler))
    judgments = client.decide(
        model="jev-1.14",
        api_key="jev-key",
        state={"composite": 0.4},
        questions=questions_for(SCREENER),
        timeout=3.0,
        battery=SCREENER,
    )
    assert seen["model"] == "jev-1.14"
    assert seen["state"] == {"composite": 0.4}
    assert "setup_direction" in seen["questions"]  # type: ignore[operator]
    assert judgments.answer("setup_direction").choice == "long"
    assert judgments.answer("setup_quality").score == 3
    assert judgments.answer("worth_deep_research").noul == 0.82
    assert judgments.model == "jev-1.14.0"
    assert judgments.model_mismatch is True
    assert judgments.request_id == "req-1"
    assert judgments.input_tokens == 120
    assert judgments.output_tokens == 40
    assert judgments.latency_ms is not None and judgments.latency_ms >= 0


def test_unknown_choice_is_rejected() -> None:
    payload = _response()
    answers = payload["answers"]
    assert isinstance(answers, dict)
    answers["setup_direction"] = {"type": "choice", "choice": "moon"}
    client = JevClient(
        transport=_transport(lambda _r: httpx.Response(200, json=payload))
    )
    with pytest.raises(JevError, match="unknown choice"):
        client.decide(
            model="jev-1.14",
            api_key="k",
            state={},
            questions=questions_for(SCREENER),
            timeout=3.0,
        )


def test_missing_or_extra_answer_is_rejected() -> None:
    payload = _response()
    answers = payload["answers"]
    assert isinstance(answers, dict)
    answers.pop("event_risk")
    client = JevClient(
        transport=_transport(lambda _r: httpx.Response(200, json=payload))
    )
    with pytest.raises(JevError, match="match"):
        client.decide(
            model="jev-1.14",
            api_key="k",
            state={},
            questions=questions_for(SCREENER),
            timeout=3.0,
        )


def test_permanent_errors_do_not_retry_and_do_not_echo_the_body() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "secret-ish"})

    client = JevClient(transport=_transport(handler))
    with pytest.raises(JevError, match="HTTP 401") as exc:
        client.decide(
            model="jev-1.14",
            api_key="k",
            state={},
            questions=questions_for(SCREENER),
            timeout=3.0,
            max_attempts=3,
        )
    assert calls == 1
    assert "secret-ish" not in str(exc.value)


def test_transient_errors_retry_within_the_attempt_budget() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = JevClient(transport=_transport(handler), sleep=lambda _seconds: None)
    with pytest.raises(JevUnavailable):
        client.decide(
            model="jev-1.14",
            api_key="k",
            state={},
            questions=questions_for(SCREENER),
            timeout=3.0,
            max_attempts=2,
        )
    assert calls == 2


def test_no_attempt_runs_when_the_budget_is_below_the_minimum() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_response())

    client = JevClient(transport=_transport(handler))
    with pytest.raises(JevUnavailable, match="budget"):
        client.decide(
            model="jev-1.14",
            api_key="k",
            state={},
            questions=questions_for(SCREENER),
            timeout=0.01,
        )
    assert calls == 0


def test_oversized_state_is_rejected_before_a_call() -> None:
    client = JevClient(transport=_transport(lambda _r: httpx.Response(500)))
    with pytest.raises(JevError, match="byte limit"):
        client.decide(
            model="jev-1.14",
            api_key="k",
            state={"x": "a" * (17 * 1024)},
            questions=questions_for(SCREENER),
            timeout=3.0,
        )


def test_endpoint_must_be_https() -> None:
    with pytest.raises(JevError, match="HTTPS"):
        JevClient(endpoint="http://insecure.example/decisions")
