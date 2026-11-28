import assert from "node:assert/strict";
import test from "node:test";

import { parseReport, parseReports } from "../src/reportContract.ts";

const report = {
  report_id: "report_1",
  report_type: "morning",
  report_date: "2026-11-26",
  status: "ON_TIME",
  title: "Morning report",
  body_md: "# Morning",
  body: { predictions: [] },
  content_hash: "abc",
  file_path: "/tmp/report.md",
  created_at: "2026-11-26T02:30:00Z",
};

test("a report list parses", () => {
  const parsed = parseReports({ reports: [report] });
  assert.equal(parsed[0].report_type, "morning");
  assert.equal(parsed[0].content_hash, "abc");
});

test("a report detail parses", () => {
  const parsed = parseReport(report);
  assert.equal(parsed.body_md, "# Morning");
});

test("malformed reports are rejected", () => {
  assert.throws(() => parseReports({ reports: [{}] }));
  assert.throws(() => parseReport({ ...report, report_type: "weekly" }));
});
