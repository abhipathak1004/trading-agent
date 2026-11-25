from dataclasses import dataclass


class BenchmarkError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BenchmarkBudget:
    name: str
    baseline_seconds: float
    allowed_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise BenchmarkError("Benchmark name is required.")
        for value in (self.baseline_seconds, self.allowed_seconds):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
            ):
                raise BenchmarkError("Benchmark durations must be nonnegative.")

    def evaluate(self, measured_seconds: float) -> bool:
        if (
            isinstance(measured_seconds, bool)
            or not isinstance(measured_seconds, (int, float))
            or measured_seconds < 0
        ):
            raise BenchmarkError("Measured benchmark duration is invalid.")
        return measured_seconds <= self.allowed_seconds


def relative_budget(
    *,
    baseline_seconds: float,
    measured_seconds: float,
    allowed_regression_fraction: float = 0.15,
) -> tuple[bool, float]:
    if (
        isinstance(baseline_seconds, bool)
        or not isinstance(baseline_seconds, (int, float))
        or baseline_seconds <= 0
    ):
        raise BenchmarkError("Baseline duration must be positive.")
    if (
        isinstance(measured_seconds, bool)
        or not isinstance(measured_seconds, (int, float))
        or measured_seconds < 0
    ):
        raise BenchmarkError("Measured duration must be nonnegative.")
    if (
        isinstance(allowed_regression_fraction, bool)
        or not isinstance(allowed_regression_fraction, (int, float))
        or allowed_regression_fraction < 0
    ):
        raise BenchmarkError("Allowed regression fraction must be nonnegative.")
    regression = (measured_seconds - baseline_seconds) / baseline_seconds
    return regression <= allowed_regression_fraction, regression
