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
