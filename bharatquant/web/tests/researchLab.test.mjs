import assert from "node:assert/strict";
import test from "node:test";

import {
  parseAlphaBenchResult,
  parseArtifactList,
  parseReconcileResult,
  parseResearchEvent,
  parseResearchLabSettings,
  parseResearchRun,
  parseResearchSession,
} from "../src/researchLabContract.ts";

const session = {
  session_id: "sess_1",
  title: "Momentum",
  model_overrides: {},
  created_at_utc: "2026-11-28T00:00:00Z",
  updated_at_utc: "2026-11-28T00:00:00Z",
};

const run = {
  run_id: "run_1",
  session_id: "sess_1",
  parent_run_id: null,
  status: "completed",
  prompt: "bench",
  params: {},
  metrics: { return_bps: 12 },
  validation: { passed: true },
  data_manifest_sha256: "a".repeat(64),
  frozen_data_path: "/tmp/inputs.parquet",
  created_at_utc: "2026-11-28T00:00:00Z",
  completed_at_utc: "2026-11-28T00:01:00Z",
};

test("a session parses and requires its id", () => {
  assert.equal(parseResearchSession(session).session_id, "sess_1");
  assert.throws(() => parseResearchSession({ ...session, session_id: 7 }));
  assert.throws(() => parseResearchSession(null));
});

test("a run parses and rejects an unknown status", () => {
  const parsed = parseResearchRun(run);
  assert.equal(parsed.status, "completed");
  assert.equal(parsed.metrics.return_bps, 12);
  assert.throws(() => parseResearchRun({ ...run, status: "exploded" }));
});

test("an event tolerates optional fields", () => {
  const parsed = parseResearchEvent({
    seq: 3,
    type: "evidence_verdict",
    passed: true,
    metrics: ["return_bps", 7],
  });
  assert.equal(parsed.seq, 3);
  assert.equal(parsed.passed, true);
  assert.deepEqual(parsed.metrics, ["return_bps"]);
  assert.equal(parsed.done, false);
  assert.equal(parsed.reduced, false);
  assert.throws(() => parseResearchEvent({ seq: 1, type: 5 }));
});

test("settings keep only the four research agents", () => {
  const parsed = parseResearchLabSettings({
    research_lab: { planner: { tier: "reasoning" } },
    routes: {
      research_lab_planner: { tier: "reasoning", provider: "GEMINI", model: "m" },
      research_lab_synthesizer: { tier: "flash", provider: null, model: null },
      unrelated_agent: { tier: "decision", provider: null, model: null },
    },
  });
  assert.deepEqual(Object.keys(parsed.routes).sort(), [
    "research_lab_planner",
    "research_lab_synthesizer",
  ]);
  assert.equal(parsed.routes.research_lab_planner.model, "m");
});

test("an artifact list reports the FTS fallback", () => {
  const parsed = parseArtifactList({
    artifacts: [
      {
        artifact_id: "art_1",
        slug: "momentum",
        revision: 2,
        parent_revision: 1,
        title: "v2",
        body_md: "IC 0.04",
        created_at_utc: "2026-11-28T00:00:00Z",
      },
    ],
    fts_available: false,
  });
  assert.equal(parsed.artifacts.length, 1);
  assert.equal(parsed.artifacts[0].parent_revision, 1);
  assert.equal(parsed.fts_available, false);
});

test("reconciliation exposes the exact discrepancy", () => {
  const parsed = parseReconcileResult({
    passed: false,
    discrepancies: [
      { field: "fees_paise", computed: 1187, reported: 1287, difference: 100 },
    ],
    computed_summary: { nav_paise: 2048813 },
  });
  assert.equal(parsed.passed, false);
  assert.equal(parsed.discrepancies[0].difference, 100);
});

test("the bench result carries the survivorship stamp", () => {
  const parsed = parseAlphaBenchResult({
    horizons: [
      { horizon_bars: 1, ic_mean: 0.03, rank_ic_mean: 0.02, icir: null, turnover: 0.1, dates: 40 },
    ],
    _meta: {
      survivorship_bias_warning: true,
      membership: "current",
      universe_size: 100,
      source: "equity_master",
      missing_symbols: ["DELISTED"],
    },
  });
  assert.equal(parsed.meta.survivorship_bias_warning, true);
  assert.equal(parsed.meta.universe_size, 100);
  assert.equal(parsed.horizons[0].icir, null);
  assert.deepEqual(parsed.meta.missing_symbols, ["DELISTED"]);
});
