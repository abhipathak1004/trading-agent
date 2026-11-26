import assert from "node:assert/strict";
import test from "node:test";

import { parseAnalysisRun, parseVendorChains } from "../src/analysisContract.ts";

const run = {
  run_id: "abc123",
  symbol: "NSE:RELIANCE",
  timeframe: "swing",
  mode: "FULL",
  as_of: "2026-11-25T09:15:00Z",
  data_provenance: {
    vendor_daily: "yahoo",
    vendor_quotes: "yahoo",
    is_realtime: false,
    data_freshness: "delayed_yfinance",
  },
  report: {
    facts: ["Trend (+0.50): close above SMA20 1297.94"],
    composite: 0.27,
    sub_scores: {
      trend: 1.0,
      momentum: 0.65,
      volatility: -0.58,
      structure: 0.0,
    },
  },
  plan: {
    signal: "BUY",
    symbol: "NSE:RELIANCE",
    exchange: "NSE",
    product: "CNC",
    order_type: "LIMIT",
    entry_price: "1351.00",
    stop_loss: "1306.47",
    target_price: "1417.80",
    reward_risk: "1.50",
    suggested_quantity: 224,
    confidence: 0.4,
    timeframe: "swing",
    invalidation_reason: "Closes below swing support.",
    data_freshness: "delayed_yfinance",
    as_of: "2026-11-25T09:15:00Z",
  },
  vetoes: [],
  notes: ["news context: not wired in this slice"],
};

test("a valid analysis run is accepted", () => {
  const parsed = parseAnalysisRun(run);
  assert.equal(parsed.symbol, "NSE:RELIANCE");
  assert.equal(parsed.plan?.order_type, "LIMIT");
  assert.equal(parsed.plan?.suggested_quantity, 224);
  assert.equal(parsed.facts.length, 1);
  assert.equal(parsed.data_provenance.data_freshness, "delayed_yfinance");
});

test("a run without a plan is accepted", () => {
  const parsed = parseAnalysisRun({ ...run, plan: null, vetoes: ["no_plan"] });
  assert.equal(parsed.plan, null);
  assert.deepEqual(parsed.vetoes, ["no_plan"]);
});

test("null sub-scores are accepted (insufficient history)", () => {
  const parsed = parseAnalysisRun({
    ...run,
    report: {
      ...run.report,
      composite: null,
      sub_scores: {
        trend: null,
        momentum: null,
        volatility: null,
        structure: null,
      },
    },
  });
  assert.equal(parsed.sub_scores.trend, null);
  assert.equal(parsed.composite, null);
});

test("a non-LIMIT plan is rejected", () => {
  assert.throws(() =>
    parseAnalysisRun({
      ...run,
      plan: { ...run.plan, order_type: "MARKET" },
    }),
  );
});

test("a plan with non-positive quantity is rejected", () => {
  assert.throws(() =>
    parseAnalysisRun({
      ...run,
      plan: { ...run.plan, suggested_quantity: 0 },
    }),
  );
});

test("malformed runs are rejected", () => {
  for (const bad of [
    { ...run, data_provenance: { ...run.data_provenance, is_realtime: "no" } },
    { ...run, report: { ...run.report, facts: [1] } },
    {
      ...run,
      report: {
        ...run.report,
        sub_scores: { ...run.report.sub_scores, trend: "up" },
      },
    },
    { ...run, vetoes: [1] },
  ]) {
    assert.throws(() => parseAnalysisRun(bad));
  }
});

test("vendor chains parse and report kite status", () => {
  const parsed = parseVendorChains({
    kite_enabled: false,
    chains: { "bars.daily": ["yahoo"], "quotes.live": ["yahoo"] },
  });
  assert.equal(parsed.kite_enabled, false);
  assert.equal(parsed.chains[0].vendors[0], "yahoo");
});

test("malformed vendor chains are rejected", () => {
  assert.throws(() => parseVendorChains({ kite_enabled: "yes", chains: {} }));
  assert.throws(() =>
    parseVendorChains({ kite_enabled: false, chains: { "bars.daily": [1] } }),
  );
});

// -- batch and agent trace -------------------------------------------------

import {
  parseBatchAnalysis,
} from "../src/analysisContract.ts";

const agentEntry = {
  node: "technical",
  role: "Technical analyst",
  status: "ok",
  summary: "4/4 dimensions scored, composite +0.27",
};

const analysisWithAgents = {
  ...run,
  agents: [agentEntry],
};

const batch = {
  timeframe: "swing",
  as_of: "2026-11-25T09:15:00Z",
  requested: 2,
  succeeded: 1,
  failed: 1,
  items: [
    {
      requested: "NSE:RELIANCE",
      symbol: "NSE:RELIANCE",
      ok: true,
      error: null,
      analysis: analysisWithAgents,
    },
    {
      requested: "BOGUS",
      symbol: "BOGUS",
      ok: false,
      error: "invalid symbol: Symbol is empty.",
      analysis: null,
    },
  ],
};

test("a valid batch is accepted with a mixed outcome", () => {
  const parsed = parseBatchAnalysis(batch);
  assert.equal(parsed.requested, 2);
  assert.equal(parsed.succeeded, 1);
  assert.equal(parsed.failed, 1);
  assert.equal(parsed.items[0].analysis.agents[0].role, "Technical analyst");
  assert.equal(parsed.items[1].analysis, null);
  assert.match(parsed.items[1].error, /invalid symbol/);
});

test("an absent agent list is tolerated as empty", () => {
  const parsed = parseBatchAnalysis({
    ...batch,
    items: [
      {
        requested: "NSE:RELIANCE",
        symbol: "NSE:RELIANCE",
        ok: true,
        error: null,
        analysis: run, // no agents field
      },
    ],
  });
  assert.deepEqual(parsed.items[0].analysis.agents, []);
});

test("a batch with a known agent status parses", () => {
  const parsed = parseBatchAnalysis({
    ...batch,
    items: [
      {
        requested: "NSE:RELIANCE",
        symbol: "NSE:RELIANCE",
        ok: true,
        error: null,
        analysis: {
          ...run,
          agents: [{ ...agentEntry, status: "degraded", role: "Bull advocate" }],
        },
      },
    ],
  });
  assert.equal(parsed.items[0].analysis.agents[0].status, "degraded");
});

test("an unknown agent status is rejected", () => {
  assert.throws(() =>
    parseBatchAnalysis({
      ...batch,
      items: [
        {
          requested: "NSE:RELIANCE",
          symbol: "NSE:RELIANCE",
          ok: true,
          error: null,
          analysis: { ...run, agents: [{ ...agentEntry, status: "weird" }] },
        },
      ],
    }),
  );
});

test("a successful item without analysis is rejected", () => {
  assert.throws(() =>
    parseBatchAnalysis({
      ...batch,
      items: [
        {
          requested: "NSE:RELIANCE",
          symbol: "NSE:RELIANCE",
          ok: true,
          error: null,
          analysis: null,
        },
      ],
    }),
  );
});

test("a failed item without an error is rejected", () => {
  assert.throws(() =>
    parseBatchAnalysis({
      ...batch,
      items: [
        {
          requested: "BOGUS",
          symbol: "BOGUS",
          ok: false,
          error: null,
          analysis: null,
        },
      ],
    }),
  );
});

test("malformed batch envelopes are rejected", () => {
  for (const bad of [
    { ...batch, requested: "two" },
    { ...batch, items: "nope" },
    { ...batch, timeframe: 5 },
  ]) {
    assert.throws(() => parseBatchAnalysis(bad));
  }
});

// -- regression: the batch payload has no run_id ----------------------------
//
// The pipeline's to_dict() has no run_id; only the run store mints one. The
// original fixture re-used an object that happened to carry one, so the UI
// rejected every real batch with "Invalid analysis run". These tests use a
// payload shaped like the actual API response.

const { run_id: _omitted, ...withoutRunId } = analysisWithAgents;

test("an analysis without a run_id is accepted", () => {
  const parsed = parseAnalysisRun(withoutRunId);
  assert.equal(parsed.run_id, undefined);
  assert.equal(parsed.symbol, "NSE:RELIANCE");
  assert.ok(parsed.plan);
});

test("a batch of payloads without run_ids parses end to end", () => {
  const parsed = parseBatchAnalysis({
    ...batch,
    items: [
      {
        requested: "NSE:RELIANCE",
        symbol: "NSE:RELIANCE",
        ok: true,
        error: null,
        analysis: withoutRunId,
      },
    ],
  });
  assert.equal(parsed.items[0].analysis.run_id, undefined);
  assert.equal(parsed.items[0].analysis.agents.length, 1);
});

test("a non-string run_id is still rejected", () => {
  assert.throws(() => parseAnalysisRun({ ...analysisWithAgents, run_id: 42 }));
});

const specialistBriefs = [
  {
    agent: "news",
    symbol: "NSE:RELIANCE",
    as_of: "2026-11-25T09:15:00Z",
    facts: ["News 2026-11-24: Reliance wins an order [CORPORATE]"],
    claims: [
      { statement: "News item: Reliance wins an order", evidence_ids: ["n1"], verified: false },
    ],
    degraded: false,
    source: "news_store",
  },
  {
    agent: "market_regime",
    symbol: "INDEX:NIFTY 50",
    as_of: "2026-11-25T09:15:00Z",
    facts: ["Regime: trend_up."],
    claims: [],
    degraded: false,
    source: "indices:yahoo",
  },
];

test("specialist briefs are parsed and optional", () => {
  assert.deepEqual(parseAnalysisRun(run).specialists, []);
  const parsed = parseAnalysisRun({ ...run, specialists: specialistBriefs });
  assert.equal(parsed.specialists.length, 2);
  assert.equal(parsed.specialists[0].agent, "news");
  assert.equal(parsed.specialists[0].claims[0].evidence_ids[0], "n1");
  assert.equal(parsed.specialists[1].degraded, false);
});

test("malformed specialist briefs are rejected", () => {
  for (const brief of [
    { ...specialistBriefs[0], facts: Array.from({ length: 11 }, () => "f") },
    { ...specialistBriefs[0], degraded: "no" },
    { ...specialistBriefs[0], claims: [{ statement: "x", evidence_ids: [] }] },
    { ...specialistBriefs[0], facts: [1] },
  ]) {
    assert.throws(() => parseAnalysisRun({ ...run, specialists: [brief] }));
  }
  assert.throws(() =>
    parseAnalysisRun({
      ...run,
      specialists: Array.from({ length: 5 }, () => specialistBriefs[0]),
    }),
  );
});

const finalGate = {
  action: "BUY",
  execute: true,
  size_scale: 0.75,
  reason: "p_exec 0.78",
  conviction: 4,
  source: "llm_fallback",
  provider: "GEMINI",
  model: "gemini-2.5-flash",
  tier: "flash",
  fallback_used: true,
  fallback_from: "JevUnavailable",
  latency_ms: 120.5,
  request_id: null,
  model_mismatch: false,
  state_hash: "abc",
};

test("the final gate is parsed and optional", () => {
  assert.equal(parseAnalysisRun(run).final_gate, null);
  const parsed = parseAnalysisRun({ ...run, final_gate: finalGate });
  assert.equal(parsed.final_gate?.execute, true);
  assert.equal(parsed.final_gate?.action, "BUY");
  assert.equal(parsed.final_gate?.source, "llm_fallback");
});

test("malformed final gates are rejected", () => {
  for (const override of [
    { execute: "yes" },
    { action: "" },
    { conviction: -1 },
    { latency_ms: -1 },
    { fallback_from: 3 },
  ]) {
    assert.throws(() =>
      parseAnalysisRun({ ...run, final_gate: { ...finalGate, ...override } }),
    );
  }
});

const evidence = {
  verified_claims: 1,
  unverified_claims: 1,
  missing_evidence_ids: ["n2"],
};

test("evidence verification is parsed and optional", () => {
  assert.equal(parseAnalysisRun(run).evidence, null);
  const parsed = parseAnalysisRun({ ...run, evidence });
  assert.equal(parsed.evidence?.verified_claims, 1);
  assert.equal(parsed.evidence?.unverified_claims, 1);
  assert.deepEqual(parsed.evidence?.missing_evidence_ids, ["n2"]);
});

test("malformed evidence verification is rejected", () => {
  for (const override of [
    { verified_claims: -1 },
    { unverified_claims: 1.5 },
    { missing_evidence_ids: [1] },
  ]) {
    assert.throws(() =>
      parseAnalysisRun({ ...run, evidence: { ...evidence, ...override } }),
    );
  }
});

test("a confidence assessment parses", () => {
  const parsed = parseAnalysisRun({
    ...run,
    confidence: {
      probability: 0.81,
      level: "VERY_HIGH",
      disposition: "SUGGEST",
      confirming_families: 4,
      base_rate_samples: 120,
      live_outcomes: 0,
      calibrated: false,
      ev_r_after_costs: 1.4,
      vetoes: [],
      evidence: [
        { family: "STRUCTURE", state: "CONFIRM", strength: 0.8 },
        { family: "VOLUME", state: "CONFIRM", strength: 0.5 },
      ],
      base_rate: {
        setup_id: "trend_continuation_v1",
        symbol: "NSE:RELIANCE",
        regime: "ALL",
        horizon_days: 10,
        sample_count: 120,
        win_count: 104,
        win_rate: 0.86,
        data_as_of: "2026-11-25T00:00:00Z",
      },
    },
  });
  assert.equal(parsed.confidence?.level, "VERY_HIGH");
  assert.equal(parsed.confidence?.base_rate.sample_count, 120);
});

test("a missing confidence assessment is accepted", () => {
  assert.equal(parseAnalysisRun(run).confidence, null);
});
