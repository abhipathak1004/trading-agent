from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bq.analysis.technical import SubScore, TechnicalReport
from bq.compute.indicators import (
    Bollinger,
    IndicatorSnapshot,
    Macd,
    SwingLevels,
    VolumeStats,
)
from bq.decision.battery import (
    EVIDENCE,
    FINAL_GATE,
    SCREENER,
    decision_prompt,
    decision_schema,
    questions_for,
)
from bq.decision.state import (
    MAX_STATE_BYTES,
    DecisionState,
    DecisionStateError,
    build_decision_state,
)

NOW = datetime(2026, 11, 26, 9, 30, tzinfo=UTC)


def _report() -> TechnicalReport:
    indicator = IndicatorSnapshot(
        as_of=NOW,
        bar_count=120,
        close=100.0,
        sma_20=98.0,
        sma_50=95.0,
        ema_20=99.0,
        rsi_14=58.1234567,
        macd=Macd(macd=1.2, signal=0.8, histogram=0.4),
        bollinger=Bollinger(middle=98.0, upper=102.0, lower=94.0, bandwidth=0.08),
        atr_14=2.5,
        vwap=97.5,
        adx_14=27.0,
        max_drawdown=0.12,
        volume=VolumeStats(latest=1200.0, average=1000.0, relative=1.2),
        swings=SwingLevels(
            support=95.0,
            resistance=105.0,
            recent_high=101.0,
            recent_low=96.0,
            close=100.0,
        ),
    )
    return TechnicalReport(
        symbol="NSE:RELIANCE",
        as_of=NOW,
        timeframe="swing",
        last_close=Decimal("100.00"),
        bar_count=120,
        trend=SubScore(0.6, ("close above sma_50",)),
        momentum=SubScore(0.4, ("rsi above 50",)),
        volatility=SubScore(-0.2, ("atr elevated",)),
        structure=SubScore(0.5, ("support below",)),
        indicators=indicator,
    )


def test_state_is_dense_deterministic_and_rounded() -> None:
    state = build_decision_state(_report(), data_freshness="delayed_yfinance")
    first = state.to_state_dict()
    second = state.to_state_dict()
    assert first == second
    assert (
        state.digest()
        == build_decision_state(_report(), data_freshness="delayed_yfinance").digest()
    )
    assert first["as_of"].endswith("+00:00")  # type: ignore[union-attr]
    assert first["rsi_14"] == 58.123457
    assert first["support_distance_pct"] == -0.05
    assert first["resistance_distance_pct"] == 0.05
    assert first["composite"] == 0.325
    assert "facts" not in first
    assert "evidence" not in first


def test_state_drops_unavailable_values() -> None:
    report = _report()
    indicator = report.indicators
    stripped = IndicatorSnapshot(
        as_of=indicator.as_of,
        bar_count=indicator.bar_count,
        close=indicator.close,
        sma_20=None,
        sma_50=None,
        ema_20=None,
        rsi_14=None,
        macd=None,
        bollinger=None,
        atr_14=None,
        vwap=None,
        adx_14=None,
        max_drawdown=None,
        volume=None,
        swings=None,
    )
    report = TechnicalReport(
        symbol=report.symbol,
        as_of=report.as_of,
        timeframe=report.timeframe,
        last_close=report.last_close,
        bar_count=report.bar_count,
        trend=None,
        momentum=None,
        volatility=None,
        structure=None,
        indicators=stripped,
    )
    payload = build_decision_state(
        report, data_freshness="delayed_yfinance"
    ).to_state_dict()
    assert "rsi_14" not in payload
    assert "composite" not in payload
    assert payload["bar_count"] == 120


def test_state_rejects_an_oversized_payload() -> None:
    with pytest.raises(DecisionStateError, match="byte limit"):
        DecisionState._check_size({"x": "a" * (MAX_STATE_BYTES + 1)})


def test_batteries_are_atomic_and_bounded() -> None:
    screener = questions_for(SCREENER)
    assert [question.name for question in screener] == [
        "setup_direction",
        "setup_quality",
        "worth_deep_research",
        "event_risk",
    ]
    assert screener[0].to_request()["type"] == "choice"  # type: ignore[union-attr]
    assert screener[1].to_request()["type"] == "score"  # type: ignore[union-attr]
    assert screener[2].to_request()["type"] == "noul"  # type: ignore[union-attr]

    final = questions_for(FINAL_GATE)
    assert final[0].to_request()["criteria"] == {
        "BUY": None,
        "SELL": None,
        "HOLD": None,
    }  # type: ignore[union-attr]

    evidence = questions_for(EVIDENCE, claims=["a claim", "another claim"])
    assert [question.name for question in evidence] == ["claim_1", "claim_2"]
    with pytest.raises(Exception, match="claims"):
        questions_for(EVIDENCE, claims=["x"] * 21)


def test_schema_and_prompt_match_the_questions() -> None:
    questions = questions_for(SCREENER)
    schema = decision_schema(questions)
    assert schema["type"] == "object"
    assert schema["required"] == [
        "event_risk",
        "setup_direction",
        "setup_quality",
        "worth_deep_research",
    ]
    prompt = decision_prompt({"composite": 0.4}, questions)
    assert '"setup_direction"' in prompt
    assert '"composite":0.4' in prompt


def test_question_validation_rejects_bad_definitions() -> None:
    from bq.decision.types import ChoiceQuestion, QuestionError, ScoreQuestion

    with pytest.raises(QuestionError):
        ChoiceQuestion(name="bad name", instructions="x", options=("a", "b"))
    with pytest.raises(QuestionError):
        ChoiceQuestion(name="ok", instructions="x", options=("a",))
    with pytest.raises(QuestionError):
        ScoreQuestion(name="ok", instructions="x", levels=("a", "a"))
