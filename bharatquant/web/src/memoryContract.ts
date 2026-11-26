import { isCount, isRecord } from "./contracts.ts";

export type CalibrationBucket = {
  decision_source: string;
  model: string;
  count: number;
  mean_brier: number | null;
  mean_log_loss: number | null;
  expected_calibration_error: number | null;
  outcome_rate: number | null;
};

export type MemoryCalibration = {
  as_of: string;
  buckets: CalibrationBucket[];
};

function numberOrNull(value: unknown, label: string): number | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`Invalid ${label}`);
  }
  return value;
}

function parseBucket(value: unknown): CalibrationBucket {
  if (
    !isRecord(value) ||
    typeof value.decision_source !== "string" ||
    value.decision_source.length === 0 ||
    typeof value.model !== "string" ||
    value.model.length === 0 ||
    !isCount(value.count) ||
    value.count < 1
  ) {
    throw new Error("Invalid calibration bucket");
  }
  return {
    decision_source: value.decision_source,
    model: value.model,
    count: value.count,
    mean_brier: numberOrNull(value.mean_brier, "Brier score"),
    mean_log_loss: numberOrNull(value.mean_log_loss, "log loss"),
    expected_calibration_error: numberOrNull(
      value.expected_calibration_error,
      "calibration error",
    ),
    outcome_rate: numberOrNull(value.outcome_rate, "outcome rate"),
  };
}

export function parseMemoryCalibration(value: unknown): MemoryCalibration {
  if (
    !isRecord(value) ||
    typeof value.as_of !== "string" ||
    !Array.isArray(value.buckets) ||
    value.buckets.length > 100
  ) {
    throw new Error("Invalid memory calibration");
  }
  return {
    as_of: value.as_of,
    buckets: value.buckets.map(parseBucket),
  };
}
