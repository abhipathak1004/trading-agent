import assert from "node:assert/strict";
import test from "node:test";

import { parseMemoryCalibration } from "../src/memoryContract.ts";
import { parseMemorySchedule } from "../src/memoryScheduleContract.ts";

const valid = {
  as_of: "2026-11-26T12:00:00+00:00",
  buckets: [
    {
      decision_source: "jev_native",
      model: "jev-1.14.0",
      count: 10,
      mean_brier: 0.04,
      mean_log_loss: 0.2,
      expected_calibration_error: 0.03,
      outcome_rate: 0.6,
    },
  ],
};

test("a valid calibration response is accepted", () => {
  const parsed = parseMemoryCalibration(valid);
  assert.equal(parsed.buckets.length, 1);
  assert.equal(parsed.buckets[0].decision_source, "jev_native");
  assert.equal(parsed.buckets[0].mean_brier, 0.04);
});

test("malformed calibration responses are rejected", () => {
  for (const override of [
    { buckets: "no" },
    { buckets: [{ ...valid.buckets[0], count: 0 }] },
    { buckets: [{ ...valid.buckets[0], model: "" }] },
    { buckets: [{ ...valid.buckets[0], mean_brier: "x" }] },
    { as_of: 3 },
  ]) {
    assert.throws(() => parseMemoryCalibration({ ...valid, ...override }));
  }
});

test("a memory schedule parses", () => {
  const parsed = parseMemorySchedule({
    configured: true,
    enabled: false,
    schedule_id: "memory-resolve-daily",
    version: 1,
    local_time: "18:00",
    timezone: "Asia/Kolkata",
  });
  assert.equal(parsed.configured, true);
  assert.equal(parsed.enabled, false);
});

test("an absent memory schedule parses", () => {
  const parsed = parseMemorySchedule({
    configured: false,
    enabled: false,
    schedule_id: null,
    version: 0,
    local_time: null,
    timezone: null,
  });
  assert.equal(parsed.configured, false);
});
