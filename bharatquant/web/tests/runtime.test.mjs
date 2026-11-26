import assert from "node:assert/strict";
import test from "node:test";

import { parseRuntimeStatus } from "../src/runtimeContract.ts";

const status = {
  status: "ok",
  database_bytes: 1024,
  wal_bytes: 0,
  free_disk_bytes: 100000000,
  migration_count: 37,
  integrity: "ok",
  jobs: {
    pending: 1,
    running: 0,
    failed: 0,
    stale: 0,
    failed_24h: 0,
    last_event_at: null,
  },
  counts: {
    reports: 2,
    research_notes: 3,
    paper_orders: 4,
    predictions: 5,
  },
  issues: [],
  checked_at: "2026-11-26T10:00:00Z",
};

test("runtime status parses", () => {
  const parsed = parseRuntimeStatus(status);
  assert.equal(parsed.migration_count, 37);
  assert.equal(parsed.jobs.pending, 1);
  assert.equal(parsed.counts.research_notes, 3);
});

test("degraded status and issues parse", () => {
  const parsed = parseRuntimeStatus({
    ...status,
    status: "degraded",
    issues: ["stale jobs"],
  });
  assert.equal(parsed.status, "degraded");
  assert.deepEqual(parsed.issues, ["stale jobs"]);
});

test("malformed runtime status is rejected", () => {
  assert.throws(() => parseRuntimeStatus({ ...status, status: "maybe" }));
  assert.throws(() =>
    parseRuntimeStatus({ ...status, jobs: { ...status.jobs, stale: -1 } }),
  );
});
