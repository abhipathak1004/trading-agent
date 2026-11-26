"""Calibration metrics for decision probabilities.

Pure functions over ``(probability, outcome)`` pairs. The store and the JSONL
triples arrive in Phase 3; the metric surface is here now so Jev-native and
fallback answers are scored with the same definitions from day one.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

MAX_SAMPLES = 100_000
MAX_BINS = 100


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float | None
    empirical: float | None


@dataclass(frozen=True, slots=True)
class CalibrationSummary:
    count: int
    mean_brier: float | None
    mean_log_loss: float | None
    expected_calibration_error: float | None
    outcome_rate: float | None


def _validate(pairs: Sequence[tuple[float, int]]) -> list[tuple[float, int]]:
    if len(pairs) > MAX_SAMPLES:
        raise CalibrationError("Too many calibration samples.")
    cleaned: list[tuple[float, int]] = []
    for probability, outcome in pairs:
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise CalibrationError("Probabilities must be numbers.")
        value = float(probability)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise CalibrationError("Probabilities must be finite and in [0, 1].")
        if isinstance(outcome, bool) or outcome not in (0, 1):
            raise CalibrationError("Outcomes must be 0 or 1.")
        cleaned.append((value, int(outcome)))
    return cleaned


def brier_score(pairs: Sequence[tuple[float, int]]) -> float | None:
    cleaned = _validate(pairs)
    if not cleaned:
        return None
    return sum((probability - outcome) ** 2 for probability, outcome in cleaned) / len(
        cleaned
    )


def log_loss(
    pairs: Sequence[tuple[float, int]], *, epsilon: float = 1e-12
) -> float | None:
    cleaned = _validate(pairs)
    if not cleaned:
        return None
    if not 0.0 < epsilon < 0.5:
        raise CalibrationError("epsilon must be in (0, 0.5).")
    total = 0.0
    for probability, outcome in cleaned:
        clipped = min(max(probability, epsilon), 1.0 - epsilon)
        total -= outcome * math.log(clipped) + (1 - outcome) * math.log(1.0 - clipped)
    return total / len(cleaned)


def reliability_curve(
    pairs: Sequence[tuple[float, int]], *, bins: int = 10
) -> tuple[ReliabilityBin, ...]:
    cleaned = _validate(pairs)
    if not 1 <= bins <= MAX_BINS:
        raise CalibrationError("The bin count is out of range.")
    curve: list[ReliabilityBin] = []
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        if index == bins - 1:
            members = [item for item in cleaned if lower <= item[0] <= upper]
        else:
            members = [item for item in cleaned if lower <= item[0] < upper]
        count = len(members)
        if count == 0:
            curve.append(ReliabilityBin(lower, upper, 0, None, None))
            continue
        mean_predicted = sum(item[0] for item in members) / count
        empirical = sum(item[1] for item in members) / count
        curve.append(ReliabilityBin(lower, upper, count, mean_predicted, empirical))
    return tuple(curve)


def expected_calibration_error(
    pairs: Sequence[tuple[float, int]], *, bins: int = 10
) -> float | None:
    cleaned = _validate(pairs)
    if not cleaned:
        return None
    curve = reliability_curve(cleaned, bins=bins)
    total = len(cleaned)
    error = 0.0
    for entry in curve:
        if entry.count == 0 or entry.mean_predicted is None or entry.empirical is None:
            continue
        error += (entry.count / total) * abs(entry.empirical - entry.mean_predicted)
    return error


def summarize(
    pairs: Sequence[tuple[float, int]], *, bins: int = 10
) -> CalibrationSummary:
    cleaned = _validate(pairs)
    if not cleaned:
        return CalibrationSummary(0, None, None, None, None)
    return CalibrationSummary(
        count=len(cleaned),
        mean_brier=brier_score(cleaned),
        mean_log_loss=log_loss(cleaned),
        expected_calibration_error=expected_calibration_error(cleaned, bins=bins),
        outcome_rate=sum(outcome for _, outcome in cleaned) / len(cleaned),
    )


__all__ = [
    "CalibrationError",
    "CalibrationSummary",
    "ReliabilityBin",
    "brier_score",
    "expected_calibration_error",
    "log_loss",
    "reliability_curve",
    "summarize",
]
