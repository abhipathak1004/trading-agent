import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bq.analysis.debate import (
    Debate,
    DebateUnavailable,
    Verdict,
    confidence_ceiling,
)
from bq.analysis.risk_team import (
    RiskAnalyst,
    deterministic_invalidation,
)
from bq.analysis.technical import TechnicalAnalyst, TechnicalReport
from bq.data.vendors.base import Bar

START = datetime(2026, 6, 1, tzinfo=UTC)


def _bars(closes: list[float], *, volume: int = 100) -> list[Bar]:
    out = []
    for index, close in enumerate(closes):
        price = Decimal(str(round(close, 4)))
        half = Decimal("0.5")
        out.append(
            Bar(
                event_at=START + timedelta(days=index),
                open=price,
                high=price + half,
                low=max(price - half, Decimal("0.01")),
                close=price,
                volume=volume,
            )
        )
    return out


def _report(closes: list[float] | None = None) -> TechnicalReport:
    values = closes or [100.0 + index * 0.8 for index in range(120)]
    return TechnicalAnalyst().analyse(
        symbol="NSE:RELIANCE", bars=_bars(values), timeframe="swing"
    )


class FakeOutcome:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeModel:
    """Replies from a queue, one response per call, in order."""

    def __init__(self, replies: list[str] | Exception) -> None:
        self.replies = replies
        self.calls: list[dict[str, str]] = []

    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        task_id: str = "",
        task_version: str = "",
    ) -> FakeOutcome:
        self.calls.append({"prompt": prompt, "system": system, "task_id": task_id})
        if isinstance(self.replies, Exception):
            raise self.replies
        if not self.replies:
            raise RuntimeError("no canned reply left")
        return FakeOutcome(self.replies.pop(0))


BULL_JSON = json.dumps(
    {
        "argument": "Trend and momentum are aligned upward.",
        "strongest_points": ["SMA20 > SMA50"],
    }
)
BEAR_JSON = json.dumps(
    {
        "argument": "Volume is not confirming the move.",
        "strongest_points": ["volume below average"],
    }
)
JUDGE_JSON = json.dumps(
    {"verdict": "BUY", "reasoning": "Bull case is better supported.", "confidence": 0.7}
)
RISK_JSON = json.dumps(
    {
        "invalidation_reason": "Closes below the 95 swing low.",
        "early_warnings": ["momentum divergence"],
        "volatility_flag": False,
    }
)


# -- confidence ceiling ----------------------------------------------------


def test_ceiling_is_zero_when_nothing_is_scorable() -> None:
    tiny = TechnicalAnalyst().analyse(
        symbol="NSE:X", bars=_bars([100.0, 101.0]), timeframe="swing"
    )
    assert confidence_ceiling(tiny) == 0.0


def test_ceiling_is_bounded_and_never_above_the_cap() -> None:
    report = _report()
    ceiling = confidence_ceiling(report)
    assert 0.0 < ceiling <= 0.8


def test_ceiling_rises_with_a_stronger_read() -> None:
    strong = _report([100.0 + index * 0.8 for index in range(120)])
    weak = _report([100.0 - index * 0.5 for index in range(120)])
    assert confidence_ceiling(strong) > confidence_ceiling(weak)


# -- bull / bear -----------------------------------------------------------


def test_bull_and_bear_are_parsed_and_bounded() -> None:
    model = FakeModel([BULL_JSON, BEAR_JSON])
    debate = Debate(model)
    report = _report()
    bull = debate.bull(report)
    bear = debate.bear(report)
    assert bull.side == "bull"
    assert bull.points[0] == "SMA20 > SMA50"
    assert bear.side == "bear"


def test_arguments_are_truncated_to_the_limit() -> None:
    long_text = "x" * 5000
    model = FakeModel([json.dumps({"argument": long_text, "strongest_points": []})])
    argument = Debate(model).bull(_report())
    assert len(argument.argument) == 700


def test_a_fenced_json_reply_is_tolerated() -> None:
    fenced = "```json\n" + BULL_JSON + "\n```"
    model = FakeModel([fenced])
    argument = Debate(model).bull(_report())
    assert argument.points


def test_non_json_reply_is_a_typed_failure() -> None:
    model = FakeModel(["I think it looks great!"])
    with pytest.raises(DebateUnavailable):
        Debate(model).bull(_report())


def test_a_provider_failure_is_degradation_not_a_crash() -> None:
    model = FakeModel(RuntimeError("provider down"))
    with pytest.raises(DebateUnavailable):
        Debate(model).bull(_report())


def test_prompt_carries_the_attribution_source() -> None:
    model = FakeModel([BULL_JSON])
    Debate(model).bull(_report())
    # The system prompt is built from the attributed module constants.
    assert "JSON" in model.calls[0]["system"]


def test_facts_are_supplied_and_no_raw_prices_are_asked_for() -> None:
    model = FakeModel([BULL_JSON])
    Debate(model).bull(_report())
    prompt = model.calls[0]["prompt"]
    assert "FACTS" in prompt
    assert "Trend" in prompt


def test_points_are_capped() -> None:
    many = json.dumps(
        {"argument": "ok", "strongest_points": [f"p{index}" for index in range(20)]}
    )
    model = FakeModel([many])
    assert len(Debate(model).bull(_report()).points) == 4


# -- judge -----------------------------------------------------------------


def _debate_judge(payload: str, ceiling: float) -> Verdict:
    model = FakeModel([payload])
    return Debate(model).judge(
        _report(),
        bull=Debate(FakeModel([BULL_JSON])).bull(_report()),
        bear=Debate(FakeModel([BEAR_JSON])).bear(_report()),
        ceiling=ceiling,
    )


def test_judge_cannot_exceed_the_deterministic_ceiling() -> None:
    verdict = _debate_judge(
        json.dumps({"verdict": "BUY", "reasoning": "strong", "confidence": 0.99}),
        ceiling=0.3,
    )
    assert verdict.confidence == pytest.approx(0.3)
    assert verdict.clamped is True


def test_judge_may_lower_confidence() -> None:
    verdict = _debate_judge(
        json.dumps({"verdict": "HOLD", "reasoning": "weak", "confidence": 0.2}),
        ceiling=0.3,
    )
    assert verdict.confidence == pytest.approx(0.2)
    assert verdict.clamped is False


def test_judge_rejects_an_unknown_verdict() -> None:
    with pytest.raises(DebateUnavailable):
        _debate_judge(
            json.dumps({"verdict": "STRONG_BUY", "reasoning": "x", "confidence": 0.5}),
            ceiling=0.8,
        )


def test_judge_rejects_out_of_range_confidence() -> None:
    with pytest.raises(DebateUnavailable):
        _debate_judge(
            json.dumps({"verdict": "BUY", "reasoning": "x", "confidence": 1.4}),
            ceiling=0.8,
        )


def test_judge_rejects_a_missing_confidence() -> None:
    with pytest.raises(DebateUnavailable):
        _debate_judge(json.dumps({"verdict": "BUY", "reasoning": "x"}), ceiling=0.8)


# -- risk team -------------------------------------------------------------


def test_risk_assessment_is_parsed() -> None:
    model = FakeModel([RISK_JSON])
    assessment = RiskAnalyst(model).assess(_report())
    assert assessment.invalidation_reason.startswith("Closes below")
    assert assessment.early_warnings == ("momentum divergence",)
    assert assessment.volatility_flag is False


def test_risk_assessment_is_marked_advisory() -> None:
    model = FakeModel([RISK_JSON])
    payload = RiskAnalyst(model).assess(_report()).to_dict()
    assert payload["advisory_only"] is True


def test_risk_model_failure_degrades() -> None:
    model = FakeModel(RuntimeError("down"))
    with pytest.raises(DebateUnavailable):
        RiskAnalyst(model).assess(_report())


def test_warnings_are_capped() -> None:
    payload = json.dumps(
        {
            "invalidation_reason": "x",
            "early_warnings": [f"w{index}" for index in range(20)],
            "volatility_flag": True,
        }
    )
    assessment = RiskAnalyst(FakeModel([payload])).assess(_report())
    assert len(assessment.early_warnings) == 5


def test_deterministic_invalidation_exists_without_a_model() -> None:
    """RULES_ONLY still states what would invalidate the plan."""
    report = _report()
    reason = deterministic_invalidation(report)
    assert reason
    assert any(char.isdigit() for char in reason) or "negative" in reason


def test_deterministic_invalidation_never_crashes_on_thin_data() -> None:
    tiny = TechnicalAnalyst().analyse(
        symbol="NSE:X", bars=_bars([100.0, 101.0]), timeframe="swing"
    )
    assert deterministic_invalidation(tiny)
