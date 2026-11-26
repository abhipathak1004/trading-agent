import assert from "node:assert/strict";
import test from "node:test";

import {
  formatBytes,
  logEntryHeadline,
  parseLogFlushResult,
  parseLoggingStatus,
} from "../src/loggingContract.ts";

function status(overrides = {}) {
  return {
    enabled: true,
    retention_days: 14,
    flush_minutes: 15,
    directory: "/tmp/logs",
    pending_bytes: 120,
    dropped: 0,
    files: [
      {
        name: "bq-2026-11-26.jsonl",
        size_bytes: 2048,
        modified_at: "2026-11-26T12:00:00Z",
      },
    ],
    recent: [
      {
        ts: "2026-11-26T12:00:00Z",
        kind: "llm_call",
        level: "info",
        truncated: false,
        data: {
          provider: "GEMINI",
          model: "gemini-2.5-pro",
          task_id: "analysis.judge",
          ok: true,
        },
      },
    ],
    ...overrides,
  };
}

test("a valid logging status parses field by field", () => {
  const parsed = parseLoggingStatus(status());
  assert.equal(parsed.retention_days, 14);
  assert.equal(parsed.files[0].name, "bq-2026-11-26.jsonl");
  assert.equal(parsed.recent[0].kind, "llm_call");
  assert.equal(parsed.recent[0].data.provider, "GEMINI");
});

test("retention and flush bounds are enforced", () => {
  assert.throws(() => parseLoggingStatus(status({ retention_days: 0 })));
  assert.throws(() => parseLoggingStatus(status({ retention_days: 3651 })));
  assert.throws(() => parseLoggingStatus(status({ flush_minutes: 0 })));
  assert.throws(() => parseLoggingStatus(status({ flush_minutes: 1441 })));
});

test("an unknown entry kind or level is rejected", () => {
  const badKind = status();
  badKind.recent[0].kind = "something_else";
  assert.throws(() => parseLoggingStatus(badKind));

  const badLevel = status();
  badLevel.recent[0].level = "loud";
  assert.throws(() => parseLoggingStatus(badLevel));
});

test("a flush result carries the refreshed status", () => {
  const parsed = parseLogFlushResult({
    written: 2,
    files: ["bq-2026-11-26.jsonl"],
    pruned: [],
    status: status({ pending_bytes: 0 }),
  });
  assert.equal(parsed.written, 2);
  assert.deepEqual(parsed.files, ["bq-2026-11-26.jsonl"]);
  assert.equal(parsed.status.pending_bytes, 0);
});

test("headlines summarise calls and decisions", () => {
  const entry = parseLoggingStatus(status()).recent[0];
  assert.equal(
    logEntryHeadline(entry),
    "GEMINI · gemini-2.5-pro · analysis.judge · ok",
  );
  assert.equal(
    logEntryHeadline({
      ts: "2026-11-26T12:00:00Z",
      kind: "decision",
      level: "info",
      truncated: false,
      data: { scope: "trigger_outcome", symbol: "RELIANCE", status: "FILLED" },
    }),
    "trigger_outcome RELIANCE FILLED",
  );
});

test("byte sizes are human readable", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(2048), "2.0 KiB");
  assert.equal(formatBytes(5 * 1024 ** 2), "5.0 MiB");
});
