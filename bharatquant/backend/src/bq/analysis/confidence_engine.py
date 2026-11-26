"""Deterministic confidence engine over technical and specialist evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bq.analysis.briefs import SpecialistBrief
from bq.analysis.decision import Signal, TradePlan
from bq.analysis.technical import TechnicalReport
from bq.compute.setup_study import study_trend_continuation
from bq.confidence import (
    ConfidenceAssessment,
    ConfidencePolicy,
    Evidence,
    EvidenceFamily,
    EvidenceState,
    assess_confidence,
)
from bq.core import Clock, SystemClock
from bq.data.vendors.base import Bar
from bq.memory.setup_rates import SetupBaseRate, SetupBaseRateStore

SETUP_ID = "trend_continuation_v1"
HORIZON_DAYS = 10
REGIME = "ALL"


class ConfidenceEngineError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ConfidenceResult:
    assessment: ConfidenceAssessment
    evidence: tuple[Evidence, ...]
    base_rate: SetupBaseRate

    def to_dict(self) -> dict[str, object]:
        return {
            "probability": self.assessment.probability,
            "level": self.assessment.level.value,
            "disposition": self.assessment.disposition.value,
            "confirming_families": self.assessment.confirming_families,
            "base_rate_samples": self.assessment.base_rate_samples,
            "live_outcomes": self.assessment.live_outcomes,
            "calibrated": self.assessment.calibrated,
            "ev_r_after_costs": self.assessment.ev_r_after_costs,
            "vetoes": list(self.assessment.vetoes),
            "evidence": [
                {
                    "family": item.family.value,
                    "state": item.state.value,
                    "strength": item.strength,
                }
                for item in self.evidence
            ],
            "base_rate": self.base_rate.to_dict(),
        }


def _state(value: float | None, *, confirm: float, against: float) -> EvidenceState:
    if value is None:
        return EvidenceState.NEUTRAL
    if value >= confirm:
        return EvidenceState.CONFIRM
    if value <= against:
        return EvidenceState.AGAINST
    return EvidenceState.NEUTRAL


def _regime_state(
    specialists: Sequence[SpecialistBrief],
) -> EvidenceState:
    for brief in specialists:
        if brief.agent != "market_regime":
            continue
        for fact in brief.facts:
            if not fact.startswith("Regime:"):
                continue
            regime = fact[len("Regime:") :].strip().rstrip(".").lower()
            if regime == "trend_up":
                return EvidenceState.CONFIRM
            if regime in {"trend_down", "high_vol_stress"}:
                return EvidenceState.AGAINST
            return EvidenceState.NEUTRAL
    return EvidenceState.NEUTRAL


def build_evidence(
    report: TechnicalReport,
    specialists: Sequence[SpecialistBrief],
) -> tuple[Evidence, ...]:
    volume = report.indicators.volume
    volume_relative = volume.relative if volume is not None else None
    evidence = (
        Evidence(
            EvidenceFamily.STRUCTURE,
            _state(
                None if report.structure is None else report.structure.value,
                confirm=0.25,
                against=-0.25,
            ),
            abs(report.structure.value) if report.structure is not None else 0.0,
        ),
        Evidence(
            EvidenceFamily.VOLUME,
            (
                EvidenceState.CONFIRM
                if volume_relative is not None and volume_relative >= 1.2
                else (
                    EvidenceState.AGAINST
                    if volume_relative is not None and volume_relative < 0.8
                    else EvidenceState.NEUTRAL
                )
            ),
            abs(volume_relative - 1.0) if volume_relative is not None else 0.0,
        ),
        Evidence(
            EvidenceFamily.MOMENTUM,
            _state(
                None if report.momentum is None else report.momentum.value,
                confirm=0.25,
                against=-0.25,
            ),
            abs(report.momentum.value) if report.momentum is not None else 0.0,
        ),
        Evidence(
            EvidenceFamily.MARKET_CONTEXT,
            _regime_state(specialists),
            0.5,
        ),
    )
    return tuple(item for item in evidence if item.strength <= 1.0)


class ConfidenceEngine:
    def __init__(
        self,
        path: Path,
        *,
        policy: ConfidencePolicy | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.rates = SetupBaseRateStore(path, clock)
        self.policy = policy or ConfidencePolicy()
        self.clock = clock or SystemClock()

    def assess(
        self,
        *,
        bars: Sequence[Bar],
        report: TechnicalReport,
        plan: TradePlan | None,
        specialists: Sequence[SpecialistBrief],
        as_of: datetime,
    ) -> ConfidenceResult | None:
        if plan is None or plan.signal is not Signal.BUY:
            return None
        if len(bars) < 60:
            raise ConfidenceEngineError("The confidence study needs at least 60 bars.")
        if as_of.utcoffset() is None:
            raise ConfidenceEngineError("Confidence as_of must be timezone-aware.")
        rate = self.rates.latest(
            setup_id=SETUP_ID,
            symbol=report.symbol,
            horizon_days=HORIZON_DAYS,
            regime=REGIME,
            as_of=bars[-1].event_at,
        )
        if rate is None or rate.data_as_of != bars[-1].event_at:
            study = study_trend_continuation(
                report.symbol,
                bars,
                horizon_days=HORIZON_DAYS,
            )
            rate = self.rates.record(study, regime=REGIME)
        evidence = build_evidence(report, specialists)
        probability = rate.win_rate if rate.sample_count else 0.5
        ev_r = probability * float(plan.reward_risk) - (1.0 - probability)
        assessment = assess_confidence(
            evidence,
            base_probability=probability,
            base_rate_samples=rate.sample_count,
            live_outcomes=0,
            ev_r_after_costs=ev_r,
            policy=self.policy,
        )
        return ConfidenceResult(
            assessment=assessment,
            evidence=evidence,
            base_rate=rate,
        )


__all__ = [
    "ConfidenceEngine",
    "ConfidenceEngineError",
    "ConfidenceResult",
    "HORIZON_DAYS",
    "REGIME",
    "SETUP_ID",
    "build_evidence",
]
