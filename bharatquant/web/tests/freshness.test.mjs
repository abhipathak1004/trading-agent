import assert from "node:assert/strict";
import test from "node:test";

import { formatAge, parseFreshness } from "../src/freshnessContract.ts";

const query = {
  recordId: "fixture", asOf: "2026-11-22T10:00:00.000Z",
  delayed: 60, critical: 180, mode: "WARN",
};

function fixture() {
  return {
    record_id: "fixture", source_id: "source", quality_status: "ACCEPTABLE",
    event_at: "2026-11-22T09:58:58.999999Z", known_at: "2026-11-22T10:00:00Z",
    recorded_at: "2026-11-22T10:00:00Z", as_of: "2026-11-22T10:00:00Z",
    status: "DELAYED_WARN", age_microseconds: "61000001",
    delayed_after_seconds: 60, critical_after_seconds: 180, delayed_mode: "WARN",
  };
}

test("freshness retains source and exact age, including unknown event time", () => {
  const result = parseFreshness(fixture(), query);
  assert.equal(result.sourceId, "source");
  assert.equal(result.status, "DELAYED_WARN");
  assert.equal(formatAge(result.ageMicroseconds), "61.000001");
  assert.equal(formatAge("1000000"), "1");
  const unknown = { ...fixture(), event_at: null, age_microseconds: null, status: "UNKNOWN_EVENT_TIME_BLOCK" };
  assert.equal(parseFreshness(unknown, query).ageMicroseconds, null);
});

test("freshness rejects mismatched query, unsafe status and malformed age", () => {
  assert.throws(() => parseFreshness({ ...fixture(), record_id: "other" }, query));
  assert.throws(() => parseFreshness({ ...fixture(), critical_after_seconds: 61 }, query));
  assert.throws(() => parseFreshness({ ...fixture(), status: "FRESH" }, query));
  assert.throws(() => parseFreshness({ ...fixture(), quality_status: "UNASSESSED" }, query));
  assert.throws(() => parseFreshness({ ...fixture(), age_microseconds: "-1" }, query));
  assert.throws(() => parseFreshness({ ...fixture(), age_microseconds: "61000000" }, query));
  assert.throws(() => parseFreshness({ ...fixture(), event_at: "2026-11-22T09:58:58" }, query));
  assert.throws(() => parseFreshness({ ...fixture(), event_at: null }, query));
  assert.throws(() => parseFreshness({ ...fixture(), recorded_at: "2026-11-22T10:00:01Z" }, query));
});

test("freshness accepts exact threshold and delayed block mode", () => {
  assert.equal(parseFreshness({ ...fixture(), event_at: "2026-11-22T09:59:00Z", age_microseconds: "60000000", status: "FRESH" }, query).status, "FRESH");
  assert.equal(parseFreshness({ ...fixture(), event_at: "2026-11-22T09:57:00Z", age_microseconds: "180000000", status: "DELAYED_WARN" }, query).status, "DELAYED_WARN");
  assert.equal(parseFreshness({ ...fixture(), event_at: "2026-11-22T09:56:59.999999Z", age_microseconds: "180000001", status: "STALE_BLOCK" }, query).status, "STALE_BLOCK");
  assert.equal(parseFreshness({ ...fixture(), delayed_mode: "BLOCK", status: "DELAYED_BLOCK" }, { ...query, mode: "BLOCK" }).status, "DELAYED_BLOCK");
  assert.equal(parseFreshness({ ...fixture(), delayed_mode: "ALLOW", status: "DELAYED_ALLOW" }, { ...query, mode: "ALLOW" }).status, "DELAYED_ALLOW");
});
