from datetime import UTC, datetime

import pytest

from bq.strategies import (
    StrategyError,
    StrategyKind,
    StrategyLifecycle,
    StrategyVersion,
    promote_strategy,
    replay_allowed,
)


def test_strategy_lifecycle_advances_one_stage() -> None:
    strategy = StrategyVersion(
        "orb",
        "v1",
        StrategyKind.RULE,
        StrategyLifecycle.DRAFT,
        None,
    )
    backtested = promote_strategy(strategy, StrategyLifecycle.BACKTESTED)
    shadow = promote_strategy(backtested, StrategyLifecycle.PAPER_SHADOW)
    active = promote_strategy(shadow, StrategyLifecycle.PAPER_ACTIVE)
    assert active.lifecycle is StrategyLifecycle.PAPER_ACTIVE
    with pytest.raises(StrategyError, match="advance one stage"):
        promote_strategy(strategy, StrategyLifecycle.PAPER_SHADOW)


def test_rule_replay_is_valid_and_llm_replay_is_cutoff_guarded() -> None:
    rule = StrategyVersion(
        "orb",
        "v1",
        StrategyKind.RULE,
        StrategyLifecycle.PAPER_SHADOW,
        None,
    )
    allowed, label = replay_allowed(
        rule,
        replay_start=datetime(2020, 1, 1, tzinfo=UTC),
        replay_end=datetime(2020, 1, 31, tzinfo=UTC),
    )
    assert allowed is True
    assert label == "FORWARD_VALID"

    llm = StrategyVersion(
        "llm-picks",
        "v1",
        StrategyKind.LLM_DISCRETIONARY,
        StrategyLifecycle.PAPER_SHADOW,
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    allowed, label = replay_allowed(
        llm,
        replay_start=datetime(2025, 1, 1, tzinfo=UTC),
        replay_end=datetime(2025, 12, 31, tzinfo=UTC),
    )
    assert allowed is False
    assert label == "CONTAMINATED"
    allowed, label = replay_allowed(
        llm,
        replay_start=datetime(2026, 2, 1, tzinfo=UTC),
        replay_end=datetime(2026, 2, 28, tzinfo=UTC),
    )
    assert allowed is True
    assert label == "FORWARD_ONLY"
