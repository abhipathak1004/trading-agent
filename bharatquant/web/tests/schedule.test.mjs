import assert from "node:assert/strict";
import test from "node:test";

import { parseInterval, parseWeekdays } from "../src/scheduleContract.ts";

test("daily and interval schedules have explicit trigger metadata", () => {
  assert.deepEqual(parseInterval({ interval_minutes: null, window_end: null }), {
    interval_minutes: null, window_end: null,
  });

  assert.deepEqual(parseInterval({ interval_minutes: 60, window_end: "10:00" }), {
    interval_minutes: 60, window_end: "10:00",
  });
});

test("weekdays are ordered distinct integer indices from Monday", () => {
  assert.deepEqual(parseWeekdays([0, 1, 2, 3, 4]), [0, 1, 2, 3, 4]);
  for (const value of [[], [0, 0], [1, 0], [7], [true], [1.5], null]) {
    assert.throws(() => parseWeekdays(value), /Invalid schedule weekdays/);
  }
});

test("invalid or incomplete interval metadata is rejected", () => {
  for (const value of [
    {}, { interval_minutes: 0, window_end: "10:00" },
    { interval_minutes: 1441, window_end: "10:00" },
    { interval_minutes: 1.5, window_end: "10:00" },
    { interval_minutes: 60, window_end: "25:00" },
    { interval_minutes: null, window_end: "10:00" },
    { interval_minutes: 60, window_end: null },
  ]) assert.throws(() => parseInterval(value), /Invalid schedule interval/);
});
