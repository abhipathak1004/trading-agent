import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bq.reports.scoring import (
    MAX_SCORED_PREDICTIONS,
    BinaryOutcome,
    DatedBinaryPrediction,
    binary_accuracy,
    calibration_bins,
    compare_binary_baseline,
    score_binary_outcomes,
    score_timely_binary_predictions,
)


def test_binary_scoring_matches_known_values() -> None:
    scores = score_binary_outcomes(
        (BinaryOutcome(0.8, True), BinaryOutcome(0.3, False))
    )
    assert scores.count == 2
    assert scores.occurred_count == 1
    assert scores.outcome_rate == 0.5
    assert scores.mean_brier == pytest.approx(0.065)
    assert scores.mean_log_loss == pytest.approx(-(math.log(0.8) + math.log(0.7)) / 2)
    assert scores.unbounded_log_loss_count == 0
    assert scores.outcome_rate_wilson_95 == pytest.approx(
        (0.09453120573423074, 0.9054687942657693)
    )


def test_empty_and_unbounded_log_loss_are_distinct() -> None:
    empty = score_binary_outcomes(())
    assert empty.count == 0
    assert empty.mean_brier is None
    assert empty.mean_log_loss is None
    assert empty.outcome_rate_wilson_95 is None
    assert empty.unbounded_log_loss_count == 0

    scores = score_binary_outcomes((BinaryOutcome(0, True), BinaryOutcome(1, False)))
    assert scores.mean_brier == 1
    assert scores.mean_log_loss is None
    assert scores.unbounded_log_loss_count == 2
    assert (
        score_binary_outcomes(
            (BinaryOutcome(0, False), BinaryOutcome(1, True))
        ).mean_log_loss
        == 0
    )


def test_binary_accuracy_uses_explicit_threshold_and_sample_interval() -> None:
    outcomes = (BinaryOutcome(0.5, True), BinaryOutcome(0.3, True))
    result = binary_accuracy(outcomes, positive_threshold=0.5)
    assert result.correct_count == 1
    assert result.accuracy == 0.5
    assert result.accuracy_wilson_95 == pytest.approx(
        (0.09453120573423074, 0.9054687942657693)
    )
    empty = binary_accuracy((), positive_threshold=0.5)
    assert empty.accuracy is None and empty.accuracy_wilson_95 is None
    with pytest.raises(ValueError, match="threshold"):
        binary_accuracy(outcomes, positive_threshold=float("nan"))


@pytest.mark.parametrize(
    "probability", [-0.1, 1.1, float("nan"), float("inf"), True, 10**1000]
)
def test_invalid_probabilities_are_rejected(probability: float) -> None:
    with pytest.raises(ValueError, match="Probability"):
        BinaryOutcome(probability, True)


def test_invalid_outcomes_and_excessive_batches_are_rejected() -> None:
    with pytest.raises(ValueError, match="boolean"):
        BinaryOutcome(0.5, 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="count"):
        score_binary_outcomes(
            (BinaryOutcome(0.5, True),) * (MAX_SCORED_PREDICTIONS + 1)
        )


def test_calibration_assigns_boundaries_and_reports_empty_bins() -> None:
    bins = calibration_bins(
        (BinaryOutcome(0, True), BinaryOutcome(0.5, False), BinaryOutcome(1, True)),
        edges=(0, 0.25, 0.5, 1),
    )
    assert (bins[0].count, bins[0].mean_probability, bins[0].outcome_rate) == (1, 0, 1)
    assert (bins[1].count, bins[1].mean_probability, bins[1].outcome_rate) == (
        0,
        None,
        None,
    )
    assert (bins[2].count, bins[2].mean_probability, bins[2].outcome_rate) == (
        2,
        0.75,
        0.5,
    )


@pytest.mark.parametrize(
    "edges", [(0, 0.5), (0, 0.5, 0.5, 1), (0, float("nan"), 1), (False, 1)]
)
def test_invalid_calibration_edges_are_rejected(edges: tuple[float, ...]) -> None:
    with pytest.raises(ValueError, match="edges"):
        calibration_bins((), edges=edges)


def test_baseline_comparison_uses_the_same_resolved_outcomes() -> None:
    actual = (BinaryOutcome(0.8, True), BinaryOutcome(0.2, False))
    baseline = (BinaryOutcome(0.5, True), BinaryOutcome(0.5, False))
    comparison = compare_binary_baseline(actual, baseline)
    assert comparison.brier_difference == pytest.approx(-0.21)
    assert comparison.log_loss_difference == pytest.approx(
        -math.log(0.8) + math.log(0.5)
    )
    with pytest.raises(ValueError, match="align"):
        compare_binary_baseline(actual, baseline[:1])
    with pytest.raises(ValueError, match="align"):
        compare_binary_baseline(actual, (baseline[1], baseline[0]))


def test_baseline_comparison_preserves_unavailable_and_unbounded_losses() -> None:
    empty = compare_binary_baseline((), ())
    assert empty.brier_difference is None and empty.log_loss_difference is None
    comparison = compare_binary_baseline(
        (BinaryOutcome(0, True),), (BinaryOutcome(0.5, True),)
    )
    assert comparison.model.unbounded_log_loss_count == 1
    assert comparison.brier_difference == pytest.approx(0.75)
    assert comparison.log_loss_difference is None


def test_timed_scores_exclude_late_and_not_yet_known_outcomes() -> None:
    start = datetime(2026, 11, 21, 9, tzinfo=UTC)
    end = start + timedelta(hours=6)
    prediction = DatedBinaryPrediction(
        "timely",
        start - timedelta(seconds=1),
        start,
        end,
        end + timedelta(hours=1),
        BinaryOutcome(0.8, True),
    )
    late = replace(prediction, prediction_id="late", created_at=start)
    before_resolution = score_timely_binary_predictions((prediction, late), as_of=end)
    assert before_resolution.timely_pending_count == 1
    assert before_resolution.late_count == 1
    assert before_resolution.scores.count == 0
    after_resolution = score_timely_binary_predictions(
        (prediction, late), as_of=end + timedelta(hours=1)
    )
    assert after_resolution.timely_resolved_count == 1
    assert after_resolution.late_count == 1
    assert after_resolution.scores.mean_brier == pytest.approx(0.04)


def test_timed_scores_reject_invalid_or_future_evidence() -> None:
    start = datetime(2026, 11, 21, 9, tzinfo=UTC)
    prediction = DatedBinaryPrediction(
        "one",
        start - timedelta(seconds=1),
        start,
        start + timedelta(hours=1),
        None,
        None,
    )
    with pytest.raises(ValueError, match="after as_of"):
        score_timely_binary_predictions(
            (prediction,), as_of=prediction.created_at - timedelta(seconds=1)
        )
    with pytest.raises(ValueError, match="unique"):
        score_timely_binary_predictions((prediction, prediction), as_of=start)
    with pytest.raises(ValueError, match="premature"):
        replace(prediction, resolved_at=start, outcome=BinaryOutcome(0.5, True))
    with pytest.raises(ValueError, match="together"):
        replace(prediction, outcome=BinaryOutcome(0.5, True))
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(prediction, window_start=start.replace(tzinfo=None))


def test_timed_scoring_compares_actual_instants_across_dst_fold() -> None:
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    later_creation = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
    end = datetime(2026, 11, 1, 2, 30, tzinfo=zone)
    prediction = DatedBinaryPrediction("late", later_creation, start, end, None, None)
    scores = score_timely_binary_predictions((prediction,), as_of=end)
    assert scores.late_count == 1
    with pytest.raises(ValueError, match="after as_of"):
        score_timely_binary_predictions((prediction,), as_of=start)
