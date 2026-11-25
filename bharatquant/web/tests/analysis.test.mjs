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
    { ...run, run_id: "" },
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
