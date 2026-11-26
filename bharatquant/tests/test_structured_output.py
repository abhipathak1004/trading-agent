from decimal import Decimal

import pytest

from bq.decision.battery import FINAL_GATE, decision_schema, questions_for
from bq.llm_result import LLMResult
from bq.llm_service import CompletionOutcome
from bq.model_output import ModelOutputError
from bq.settings import Provider
from bq.structured_output import complete_structured


class FakeCompleter:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls = 0

    def __call__(
        self,
        *,
        prompt: str,
        system: str,
        task_id: str,
        task_version: str,
        timeout: float | None,
    ) -> CompletionOutcome:
        self.calls += 1
        text = self.texts.pop(0)
        return CompletionOutcome(
            provider=Provider.GEMINI,
            model="gemini-2.5-flash",
            result=LLMResult(text=text),
        )


def test_schema_is_strict_and_typed() -> None:
    schema = decision_schema(questions_for(FINAL_GATE))
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["final_action"] == {
        "type": "string",
        "enum": ["BUY", "SELL", "HOLD"],
    }
    assert properties["execute_trade"] == {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
    }
    assert properties["conviction_tier"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 4,
    }
    assert schema["additionalProperties"] is False


def test_valid_json_is_returned() -> None:
    completer = FakeCompleter(
        '{"final_action":"BUY","execute_trade":0.8,"conviction_tier":4}'
    )
    parsed, outcome = complete_structured(
        completer,
        prompt="state",
        system="system",
        schema=decision_schema(questions_for(FINAL_GATE)),
        task_id="decision.final_gate",
    )
    assert parsed == {
        "final_action": "BUY",
        "execute_trade": Decimal("0.8"),
        "conviction_tier": 4,
    }
    assert outcome.model == "gemini-2.5-flash"
    assert completer.calls == 1


def test_one_repair_attempt_is_allowed() -> None:
    completer = FakeCompleter(
        "not json",
        '{"final_action":"HOLD","execute_trade":0.1,"conviction_tier":1}',
    )
    parsed, _ = complete_structured(
        completer,
        prompt="state",
        system="system",
        schema=decision_schema(questions_for(FINAL_GATE)),
        task_id="decision.final_gate",
    )
    assert parsed == {
        "final_action": "HOLD",
        "execute_trade": Decimal("0.1"),
        "conviction_tier": 1,
    }
    assert completer.calls == 2


def test_invalid_json_twice_raises() -> None:
    completer = FakeCompleter("not json", "still not json")
    with pytest.raises(ModelOutputError):
        complete_structured(
            completer,
            prompt="state",
            system="system",
            schema=decision_schema(questions_for(FINAL_GATE)),
            task_id="decision.final_gate",
        )
    assert completer.calls == 2


def test_out_of_enum_value_is_rejected() -> None:
    completer = FakeCompleter(
        '{"final_action":"MOON","execute_trade":0.8,"conviction_tier":4}'
    )
    with pytest.raises(ModelOutputError):
        complete_structured(
            completer,
            prompt="state",
            system="system",
            schema=decision_schema(questions_for(FINAL_GATE)),
            task_id="decision.final_gate",
            repair_attempts=0,
        )
