from bq.decision.policy import (
    DecisionPolicy,
    FinalVerdict,
    ScreenerVerdict,
)
from bq.decision.types import Answer, JudgmentSet, QuestionType


def _screener(
    *,
    direction: str = "long",
    quality: int = 4,
    worth: float = 0.9,
    risk: float = 0.1,
    confidence: float = 0.9,
) -> JudgmentSet:
    return JudgmentSet(
        battery="screener",
        answers=(
            (
                "setup_direction",
                Answer(
                    kind=QuestionType.CHOICE,
                    choice=direction,
                    confidence=confidence,
                ),
            ),
            (
                "setup_quality",
                Answer(
                    kind=QuestionType.SCORE,
                    score=quality,
                    confidence=confidence,
                ),
            ),
            ("worth_deep_research", Answer(kind=QuestionType.NOUL, noul=worth)),
            ("event_risk", Answer(kind=QuestionType.NOUL, noul=risk)),
        ),
        model="m",
        source="jev_native",
    )


def _final(
    *,
    action: str = "BUY",
    execute: float = 0.8,
    conviction: int = 4,
    confidence: float = 0.9,
) -> JudgmentSet:
    return JudgmentSet(
        battery="final_gate",
        answers=(
            (
                "final_action",
                Answer(
                    kind=QuestionType.CHOICE,
                    choice=action,
                    confidence=confidence,
                ),
            ),
            ("execute_trade", Answer(kind=QuestionType.NOUL, noul=execute)),
            (
                "conviction_tier",
                Answer(
                    kind=QuestionType.SCORE,
                    score=conviction,
                    confidence=confidence,
                ),
            ),
        ),
        model="m",
        source="jev_native",
    )


def test_screener_researches_a_clean_setup() -> None:
    decision = DecisionPolicy().compose_screener(_screener())
    assert decision.verdict is ScreenerVerdict.RESEARCH
    assert decision.quality == 1.0
    assert decision.worth == 0.9


def test_screener_skips_stand_aside_low_quality_and_event_risk() -> None:
    policy = DecisionPolicy()
    assert policy.compose_screener(_screener(direction="stand_aside")).verdict is (
        ScreenerVerdict.SKIP
    )
    assert policy.compose_screener(_screener(quality=0, worth=0.9)).verdict is (
        ScreenerVerdict.SKIP
    )
    assert policy.compose_screener(_screener(worth=0.4)).verdict is (
        ScreenerVerdict.SKIP
    )
    assert policy.compose_screener(_screener(risk=0.95)).verdict is (
        ScreenerVerdict.SKIP
    )


def test_final_gate_executes_and_scales_by_source() -> None:
    policy = DecisionPolicy()
    native = policy.compose_final(_final(), source="jev_native")
    fallback = policy.compose_final(_final(), source="llm_fallback")
    heuristic = policy.compose_final(_final(), source="heuristic")
    assert native.verdict is FinalVerdict.BUY
    assert native.execute is True
    assert native.size_scale > fallback.size_scale > heuristic.size_scale > 0.0


def test_final_gate_blocks_hold_low_probability_abstain_and_low_confidence() -> None:
    policy = DecisionPolicy()
    assert (
        policy.compose_final(_final(action="HOLD"), source="jev_native").execute
        is False
    )
    assert (
        policy.compose_final(_final(execute=0.4), source="jev_native").execute is False
    )
    assert (
        policy.compose_final(_final(conviction=2), source="jev_native").execute is False
    )
    assert (
        policy.compose_final(_final(conviction=3), source="jev_native").execute is False
    )
    assert (
        policy.compose_final(_final(confidence=0.2), source="jev_native").execute
        is False
    )


def test_rules_only_has_no_size_budget() -> None:
    decision = DecisionPolicy().compose_final(_final(), source="rules_only")
    assert decision.execute is False
    assert decision.size_scale == 0.0


def test_sell_is_representable() -> None:
    decision = DecisionPolicy().compose_final(
        _final(action="SELL"), source="jev_native"
    )
    assert decision.verdict is FinalVerdict.SELL
    assert decision.execute is True


def test_heuristic_confidence_does_not_trip_the_native_gate() -> None:
    policy = DecisionPolicy()
    heuristic = JudgmentSet(
        battery="screener",
        answers=_screener().answers,
        model="heuristic",
        source="heuristic",
    )
    decision = policy.compose_screener(heuristic)
    assert decision.verdict is ScreenerVerdict.RESEARCH
    final = JudgmentSet(
        battery="final_gate",
        answers=_final(conviction=4).answers,
        model="heuristic",
        source="heuristic",
    )
    executed = policy.compose_final(final, source="heuristic")
    assert executed.execute is True
    assert executed.size_scale == 0.5
