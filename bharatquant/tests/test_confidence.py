import pytest

from bq.confidence import (
    ConfidenceDisposition,
    ConfidenceLevel,
    ConfidencePolicy,
    Evidence,
    EvidenceFamily,
    EvidenceState,
    assess_confidence,
)


def confirming() -> tuple[Evidence, ...]:
    return tuple(
        Evidence(family, EvidenceState.CONFIRM, 0.8)
        for family in (
            EvidenceFamily.STRUCTURE,
            EvidenceFamily.VOLUME,
            EvidenceFamily.MOMENTUM,
            EvidenceFamily.RELATIVE_STRENGTH,
        )
    )


def test_high_confidence_suggests_only_when_all_gates_pass() -> None:
    assessment = assess_confidence(
        confirming(),
        base_probability=0.75,
        base_rate_samples=500,
        live_outcomes=60,
        ev_r_after_costs=0.35,
    )
    assert assessment.level is ConfidenceLevel.HIGH
    assert assessment.disposition is ConfidenceDisposition.SUGGEST
    assert assessment.confirming_families == 4
    assert assessment.calibrated is True


def test_mandatory_evidence_vetoes_and_insufficient_history() -> None:
    evidence = tuple(
        Evidence(family, EvidenceState.CONFIRM, 0.8)
        for family in (
            EvidenceFamily.STRUCTURE,
            EvidenceFamily.MOMENTUM,
            EvidenceFamily.RELATIVE_STRENGTH,
            EvidenceFamily.MARKET_CONTEXT,
        )
    )
    assessment = assess_confidence(
        evidence,
        base_probability=0.82,
        base_rate_samples=99,
        live_outcomes=10,
        ev_r_after_costs=0.1,
    )
    assert assessment.disposition is ConfidenceDisposition.DISCARD
    assert "mandatory_structure_volume_not_confirmed" in assessment.vetoes
    assert "insufficient_history" in assessment.vetoes
    assert "expected_value_below_minimum" in assessment.vetoes


def test_provisional_haircut_applies_before_level_and_ev() -> None:
    assessment = assess_confidence(
        confirming(),
        base_probability=0.72,
        base_rate_samples=100,
        live_outcomes=59,
        ev_r_after_costs=0.31,
        policy=ConfidencePolicy(provisional_haircut_pp=0.05),
    )
    assert assessment.calibrated is False
    assert assessment.probability == pytest.approx(0.67)
    assert assessment.level is ConfidenceLevel.MODERATE
    assert assessment.disposition is ConfidenceDisposition.WATCH
