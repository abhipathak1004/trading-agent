import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime

MAX_SCORED_PREDICTIONS = 100_000
MAX_CALIBRATION_BINS = 100
WILSON_95_Z = 1.959963984540054


@dataclass(frozen=True, slots=True)
class BinaryOutcome:
    probability: float
    occurred: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.probability, bool)
            or not isinstance(self.probability, int | float)
            or not 0 <= self.probability <= 1
            or not math.isfinite(self.probability)
        ):
            raise ValueError("Probability must be a finite number between 0 and 1.")
        if not isinstance(self.occurred, bool):
            raise ValueError("Binary outcome must be a boolean.")


@dataclass(frozen=True, slots=True)
class BinaryScores:
    count: int
    occurred_count: int
    outcome_rate: float | None
    outcome_rate_wilson_95: tuple[float, float] | None
    mean_brier: float | None
    mean_log_loss: float | None
    unbounded_log_loss_count: int


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_probability: float | None
    outcome_rate: float | None


@dataclass(frozen=True, slots=True)
class BinaryBaselineComparison:
    model: BinaryScores
    baseline: BinaryScores
    brier_difference: float | None
    log_loss_difference: float | None


@dataclass(frozen=True, slots=True)
class BinaryAccuracy:
    count: int
    correct_count: int
    accuracy: float | None
    accuracy_wilson_95: tuple[float, float] | None


@dataclass(frozen=True, slots=True)
class DatedBinaryPrediction:
    prediction_id: str
    created_at: datetime
    window_start: datetime
    window_end: datetime
    resolved_at: datetime | None
    outcome: BinaryOutcome | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.prediction_id, str)
            or not self.prediction_id
            or len(self.prediction_id) > 128
        ):
            raise ValueError("Invalid prediction identifier.")
        times = (self.created_at, self.window_start, self.window_end)
        if any(
            not isinstance(value, datetime) or value.utcoffset() is None
            for value in times
        ):
            raise ValueError("Prediction times must be timezone-aware.")
        if self.window_end.astimezone(UTC) <= self.window_start.astimezone(UTC):
            raise ValueError("Prediction window must end after it starts.")
        if (self.resolved_at is None) != (self.outcome is None):
            raise ValueError(
                "Resolution time and binary outcome must be supplied together."
            )
        if self.resolved_at is not None:
            if (
                not isinstance(self.resolved_at, datetime)
                or self.resolved_at.utcoffset() is None
                or self.resolved_at.astimezone(UTC) < self.window_end.astimezone(UTC)
                or self.resolved_at.astimezone(UTC) < self.created_at.astimezone(UTC)
                or not isinstance(self.outcome, BinaryOutcome)
            ):
                raise ValueError("Prediction resolution is invalid or premature.")


@dataclass(frozen=True, slots=True)
class TimelyBinaryScores:
    timely_resolved_count: int
    timely_pending_count: int
    late_count: int
    scores: BinaryScores


def _validate_outcomes(outcomes: tuple[BinaryOutcome, ...]) -> None:
    if not isinstance(outcomes, tuple):
        raise ValueError("Binary outcomes must be a tuple of BinaryOutcome values.")
    if len(outcomes) > MAX_SCORED_PREDICTIONS:
        raise ValueError("Binary outcome count exceeds the limit.")
    if any(not isinstance(outcome, BinaryOutcome) for outcome in outcomes):
        raise ValueError("Binary outcomes must be a tuple of BinaryOutcome values.")


def _wilson_95(successes: int, count: int) -> tuple[float, float]:
    rate = successes / count
    z_squared = WILSON_95_Z**2
    denominator = 1 + z_squared / count
    center = (rate + z_squared / (2 * count)) / denominator
    radius = (
        WILSON_95_Z
        * math.sqrt(rate * (1 - rate) / count + z_squared / (4 * count**2))
        / denominator
    )
    return max(0.0, center - radius), min(1.0, center + radius)


def score_binary_outcomes(outcomes: tuple[BinaryOutcome, ...]) -> BinaryScores:
    _validate_outcomes(outcomes)
    count = len(outcomes)
    if not count:
        return BinaryScores(0, 0, None, None, None, None, 0)
    occurred_count = sum(outcome.occurred for outcome in outcomes)
    rate = occurred_count / count
    brier = (
        math.fsum(
            (outcome.probability - int(outcome.occurred)) ** 2 for outcome in outcomes
        )
        / count
    )
    unbounded = sum(
        (outcome.probability == 0 and outcome.occurred)
        or (outcome.probability == 1 and not outcome.occurred)
        for outcome in outcomes
    )
    log_loss = None
    if not unbounded:
        log_loss = (
            math.fsum(
                -math.log(outcome.probability)
                if outcome.occurred
                else -math.log1p(-outcome.probability)
                for outcome in outcomes
            )
            / count
        )
    return BinaryScores(
        count=count,
        occurred_count=occurred_count,
        outcome_rate=rate,
        outcome_rate_wilson_95=_wilson_95(occurred_count, count),
        mean_brier=brier,
        mean_log_loss=log_loss,
        unbounded_log_loss_count=unbounded,
    )


def binary_accuracy(
    outcomes: tuple[BinaryOutcome, ...], *, positive_threshold: float
) -> BinaryAccuracy:
    _validate_outcomes(outcomes)
    if (
        isinstance(positive_threshold, bool)
        or not isinstance(positive_threshold, int | float)
        or not 0 <= positive_threshold <= 1
        or not math.isfinite(positive_threshold)
    ):
        raise ValueError("Positive threshold must be a finite number from 0 to 1.")
    count = len(outcomes)
    if not count:
        return BinaryAccuracy(0, 0, None, None)
    correct = sum(
        (outcome.probability >= positive_threshold) == outcome.occurred
        for outcome in outcomes
    )
    return BinaryAccuracy(count, correct, correct / count, _wilson_95(correct, count))


def calibration_bins(
    outcomes: tuple[BinaryOutcome, ...], *, edges: tuple[float, ...]
) -> tuple[CalibrationBin, ...]:
    _validate_outcomes(outcomes)
    if (
        not isinstance(edges, tuple)
        or not 2 <= len(edges) <= MAX_CALIBRATION_BINS + 1
        or any(
            isinstance(edge, bool)
            or not isinstance(edge, int | float)
            or not 0 <= edge <= 1
            or not math.isfinite(edge)
            for edge in edges
        )
        or edges[0] != 0
        or edges[-1] != 1
        or any(left >= right for left, right in zip(edges, edges[1:]))
    ):
        raise ValueError("Calibration edges must strictly increase from 0 to 1.")
    groups: list[list[BinaryOutcome]] = [[] for _ in edges[1:]]
    for outcome in outcomes:
        index = min(bisect_right(edges, outcome.probability) - 1, len(groups) - 1)
        groups[index].append(outcome)
    return tuple(
        CalibrationBin(
            lower=edges[index],
            upper=edges[index + 1],
            count=len(group),
            mean_probability=(
                math.fsum(outcome.probability for outcome in group) / len(group)
                if group
                else None
            ),
            outcome_rate=(
                sum(outcome.occurred for outcome in group) / len(group)
                if group
                else None
            ),
        )
        for index, group in enumerate(groups)
    )


def compare_binary_baseline(
    outcomes: tuple[BinaryOutcome, ...], baseline: tuple[BinaryOutcome, ...]
) -> BinaryBaselineComparison:
    _validate_outcomes(outcomes)
    _validate_outcomes(baseline)
    if len(outcomes) != len(baseline) or any(
        model.occurred != reference.occurred
        for model, reference in zip(outcomes, baseline)
    ):
        raise ValueError("Model and baseline outcomes must align.")
    model_scores = score_binary_outcomes(outcomes)
    baseline_scores = score_binary_outcomes(baseline)
    brier_difference = (
        model_scores.mean_brier - baseline_scores.mean_brier
        if model_scores.mean_brier is not None
        and baseline_scores.mean_brier is not None
        else None
    )
    log_loss_difference = (
        model_scores.mean_log_loss - baseline_scores.mean_log_loss
        if model_scores.mean_log_loss is not None
        and baseline_scores.mean_log_loss is not None
        else None
    )
    return BinaryBaselineComparison(
        model_scores, baseline_scores, brier_difference, log_loss_difference
    )


def score_timely_binary_predictions(
    predictions: tuple[DatedBinaryPrediction, ...], *, as_of: datetime
) -> TimelyBinaryScores:
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("Scoring as_of must be timezone-aware.")
    if (
        not isinstance(predictions, tuple)
        or len(predictions) > MAX_SCORED_PREDICTIONS
        or any(not isinstance(item, DatedBinaryPrediction) for item in predictions)
    ):
        raise ValueError("Predictions must be a bounded tuple.")
    if len({item.prediction_id for item in predictions}) != len(predictions):
        raise ValueError("Prediction identifiers must be unique.")
    instant = as_of.astimezone(UTC)
    if any(item.created_at.astimezone(UTC) > instant for item in predictions):
        raise ValueError("Predictions created after as_of cannot be scored.")
    scored: list[BinaryOutcome] = []
    pending = 0
    late = 0
    for item in predictions:
        if item.created_at.astimezone(UTC) >= item.window_start.astimezone(UTC):
            late += 1
        elif item.resolved_at is None or item.resolved_at.astimezone(UTC) > instant:
            pending += 1
        else:
            if item.outcome is None:
                raise ValueError("A resolved prediction requires an outcome.")
            scored.append(item.outcome)
    return TimelyBinaryScores(
        len(scored), pending, late, score_binary_outcomes(tuple(scored))
    )
