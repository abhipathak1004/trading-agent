from dataclasses import dataclass
from enum import StrEnum


class EvidenceFamily(StrEnum):
    STRUCTURE = "STRUCTURE"
    VOLUME = "VOLUME"
    MOMENTUM = "MOMENTUM"
    RELATIVE_STRENGTH = "RELATIVE_STRENGTH"
    MARKET_CONTEXT = "MARKET_CONTEXT"
    PEER_GRAPH = "PEER_GRAPH"
    CATALYST = "CATALYST"


class EvidenceState(StrEnum):
    CONFIRM = "CONFIRM"
    NEUTRAL = "NEUTRAL"
    AGAINST = "AGAINST"


class ConfidenceLevel(StrEnum):
    VERY_HIGH = "VERY_HIGH"
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    LOW = "LOW"


class ConfidenceDisposition(StrEnum):
    SUGGEST = "SUGGEST"
    WATCH = "WATCH"
    DISCARD = "DISCARD"


class ConfidenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Evidence:
    family: EvidenceFamily
    state: EvidenceState
    strength: float

    def __post_init__(self) -> None:
        if not isinstance(self.family, EvidenceFamily):
            raise ConfidenceError("Invalid evidence family.")
        if not isinstance(self.state, EvidenceState):
            raise ConfidenceError("Invalid evidence state.")
        if (
            isinstance(self.strength, bool)
            or not isinstance(self.strength, (int, float))
            or not 0 <= self.strength <= 1
        ):
            raise ConfidenceError("Evidence strength must be from 0 to 1.")


@dataclass(frozen=True, slots=True)
class ConfidencePolicy:
    min_base_rate_samples: int = 100
    min_families_confirming: int = 4
    very_high_probability: float = 0.80
    high_probability: float = 0.70
    moderate_probability: float = 0.60
    min_ev_r: float = 0.30
    provisional_min_live_outcomes: int = 60
    provisional_haircut_pp: float = 0.05

    def __post_init__(self) -> None:
        if (
            isinstance(self.min_base_rate_samples, bool)
            or not isinstance(self.min_base_rate_samples, int)
            or self.min_base_rate_samples < 1
        ):
            raise ConfidenceError("Minimum base-rate samples must be positive.")
        if (
            isinstance(self.min_families_confirming, bool)
            or not isinstance(self.min_families_confirming, int)
            or not 1 <= self.min_families_confirming <= 7
        ):
            raise ConfidenceError("Minimum confirming families must be 1–7.")
        probabilities = (
            self.very_high_probability,
            self.high_probability,
            self.moderate_probability,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1
            for value in probabilities
        ):
            raise ConfidenceError("Confidence thresholds must be from 0 to 1.")
        if not (
            0
            <= self.moderate_probability
            <= self.high_probability
            <= self.very_high_probability
            <= 1
        ):
            raise ConfidenceError("Confidence thresholds must be ordered.")
        if (
            isinstance(self.provisional_haircut_pp, bool)
            or not isinstance(self.provisional_haircut_pp, (int, float))
            or not 0 <= self.provisional_haircut_pp <= 1
        ):
            raise ConfidenceError("Provisional haircut must be from 0 to 1.")


@dataclass(frozen=True, slots=True)
class ConfidenceAssessment:
    probability: float
    level: ConfidenceLevel
    disposition: ConfidenceDisposition
    confirming_families: int
    base_rate_samples: int
    live_outcomes: int
    calibrated: bool
    ev_r_after_costs: float
    vetoes: tuple[str, ...]


def assess_confidence(
    evidence: tuple[Evidence, ...],
    *,
    base_probability: float,
    base_rate_samples: int,
    live_outcomes: int,
    ev_r_after_costs: float,
    vetoes: tuple[str, ...] = (),
    policy: ConfidencePolicy | None = None,
) -> ConfidenceAssessment:
    active_policy = policy or ConfidencePolicy()
    if (
        not isinstance(evidence, tuple)
        or len(evidence) > 7
        or any(not isinstance(item, Evidence) for item in evidence)
    ):
        raise ConfidenceError("Evidence must be a bounded tuple.")
    seen: set[EvidenceFamily] = set()
    confirming = 0
    against = 0
    states: dict[EvidenceFamily, EvidenceState] = {}
    for item in evidence:
        if item.family in seen:
            raise ConfidenceError("Evidence families must be unique.")
        seen.add(item.family)
        states[item.family] = item.state
        if item.state is EvidenceState.CONFIRM:
            confirming += 1
        elif item.state is EvidenceState.AGAINST:
            against += 1
    if not vetoes:
        vetoes = tuple()
    if not isinstance(vetoes, tuple) or any(
        not isinstance(item, str) or not item for item in vetoes
    ):
        raise ConfidenceError("Vetoes must be a bounded tuple of reasons.")
    if len(vetoes) > 32:
        raise ConfidenceError("Too many veto reasons.")
    if (
        isinstance(base_probability, bool)
        or not isinstance(base_probability, (int, float))
        or not 0 <= base_probability <= 1
    ):
        raise ConfidenceError("Base probability must be from 0 to 1.")
    if (
        isinstance(base_rate_samples, bool)
        or not isinstance(base_rate_samples, int)
        or base_rate_samples < 0
    ):
        raise ConfidenceError("Base-rate sample count must be nonnegative.")
    if (
        isinstance(live_outcomes, bool)
        or not isinstance(live_outcomes, int)
        or live_outcomes < 0
    ):
        raise ConfidenceError("Live-outcome count must be nonnegative.")
    if isinstance(ev_r_after_costs, bool) or not isinstance(
        ev_r_after_costs, (int, float)
    ):
        raise ConfidenceError("EV/R must be numeric.")

    calibrated = live_outcomes >= active_policy.provisional_min_live_outcomes
    probability = float(base_probability)
    if not calibrated:
        probability = max(0.0, probability - active_policy.provisional_haircut_pp)

    vetoed = bool(vetoes)
    if base_rate_samples < active_policy.min_base_rate_samples:
        vetoed = True
        vetoes = (*vetoes, "insufficient_history")
    if (
        states.get(EvidenceFamily.STRUCTURE) is not EvidenceState.CONFIRM
        or states.get(EvidenceFamily.VOLUME) is not EvidenceState.CONFIRM
    ):
        vetoed = True
        vetoes = (*vetoes, "mandatory_structure_volume_not_confirmed")
    if confirming < active_policy.min_families_confirming:
        vetoed = True
        vetoes = (*vetoes, "minimum_families_not_confirmed")
    if against > 0:
        vetoed = True
        vetoes = (*vetoes, "evidence_against")
    if ev_r_after_costs < active_policy.min_ev_r:
        vetoed = True
        vetoes = (*vetoes, "expected_value_below_minimum")

    if vetoed:
        level = (
            ConfidenceLevel.LOW
            if probability < active_policy.moderate_probability
            else ConfidenceLevel.MODERATE
        )
        disposition = ConfidenceDisposition.DISCARD
    elif probability >= active_policy.very_high_probability:
        level = ConfidenceLevel.VERY_HIGH
        disposition = ConfidenceDisposition.SUGGEST
    elif probability >= active_policy.high_probability:
        level = ConfidenceLevel.HIGH
        disposition = ConfidenceDisposition.SUGGEST
    elif probability >= active_policy.moderate_probability:
        level = ConfidenceLevel.MODERATE
        disposition = ConfidenceDisposition.WATCH
    else:
        level = ConfidenceLevel.LOW
        disposition = ConfidenceDisposition.DISCARD

    return ConfidenceAssessment(
        probability=probability,
        level=level,
        disposition=disposition,
        confirming_families=confirming,
        base_rate_samples=base_rate_samples,
        live_outcomes=live_outcomes,
        calibrated=calibrated,
        ev_r_after_costs=float(ev_r_after_costs),
        vetoes=tuple(dict.fromkeys(vetoes)),
    )
