import assert from "node:assert/strict";
import test from "node:test";

import {
  parseJobCatalogue,
  parseJobRun,
  parseScheduleList,
} from "../src/schedulerContract.ts";

const job = {
  job_id: "memory.resolve",
  label: "Resolve memory outcomes",
  description: "Resolve due episodes.",
  resource_class: "LIGHT",
  timezone: "Asia/Kolkata",
  local_time: "18:00",
  window_start: "17:00",
  window_end: "19:00",
  enabled_by_default: false,
  max_attempts: 2,
  wake_policy: "ALWAYS_RUN",
};

const schedule = {
  schedule_id: "memory-resolve-daily",
  job_id: "memory.resolve",
  enabled: true,
  timezone: "Asia/Kolkata",
  local_time: "18:00",
  window_start: "17:00",
  window_end: "19:00",
  wake_policy: "ALWAYS_RUN",
  wake_within_seconds: null,
  max_attempts: 2,
  interval_minutes: null,
  weekdays: [0, 1, 2, 3, 4, 5, 6],
  dependencies: [],
  version: 1,
  next_occurrences: ["2026-11-26T12:30:00Z"],
};

test("a job catalogue parses", () => {
  const parsed = parseJobCatalogue({ jobs: [job] });
  assert.equal(parsed[0].job_id, "memory.resolve");
  assert.equal(parsed[0].resource_class, "LIGHT");
});

test("a schedule list parses", () => {
  const parsed = parseScheduleList({ version: 1, schedules: [schedule] });
  assert.equal(parsed.schedules[0].schedule_id, "memory-resolve-daily");
  assert.equal(parsed.schedules[0].next_occurrences.length, 1);
});

test("a job run parses", () => {
  const parsed = parseJobRun({
    run_id: "run-1",
    job_id: "memory.resolve",
    occurrence_key: "run-now:memory-resolve-daily:ui-1",
    scheduled_at: "2026-11-26T12:00:00Z",
    status: "PENDING",
    attempts: 0,
    max_attempts: 2,
    failure_code: null,
  });
  assert.equal(parsed.status, "PENDING");
});

test("malformed scheduler payloads are rejected", () => {
  assert.throws(() => parseJobCatalogue({ jobs: [{}] }));
  assert.throws(() => parseScheduleList({ version: 1, schedules: [{}] }));
  assert.throws(() => parseJobRun({ run_id: "x" }));
});
