import assert from "node:assert/strict";
import test from "node:test";

import {
  parseOpportunityList,
  parseShadowSummary,
} from "../src/opportunityContract.ts";

const plan = {
  signal: "BUY",
  symbol: "NSE:RELIANCE",
  exchange: "NSE",
  product: "CNC",
  order_type: "LIMIT",
  entry_price: "100.00",
  stop_loss: "95.00",
  target_price: "110.00",
  reward_risk: "2.00",
  suggested_quantity: 10,
  confidence: 0.7,
  timeframe: "swing",
  invalidation_reason: "close below support",
  data_freshness: "delayed_yfinance",
  as_of: "2026-11-26T12:00:00Z",
};
const opportunity = {
  opportunity_id: "opp_1",
  instrument_id: "NSE:RELIANCE",
  horizon: "SWING",
  side: "BUY",
  created_at: "2026-11-26T12:00:00Z",
  probability: 0.81,
  level: "VERY_HIGH",
  disposition: "SUGGEST",
  confirming_families: 4,
  base_rate_samples: 120,
  calibrated: false,
  ev_r_after_costs: 1.4,
  vetoes: [],
  assessment_hash: "abc123",
  score: 78.2,
  run_id: "run-1",
  source: "analysis",
  setup_id: "trend_continuation_v1",
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
    evidence: [{ family: "STRUCTURE", state: "CONFIRM", strength: 0.8 }],
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
  plan,
  lifecycle: "NEW",
};

test("an opportunity list parses", () => {
  const parsed = parseOpportunityList({ opportunities: [opportunity] });
  assert.equal(parsed.opportunities[0].level, "VERY_HIGH");
  assert.equal(parsed.opportunities[0].plan?.entry_price, "100.00");
  assert.equal(parsed.opportunities[0].confidence?.confirming_families, 4);
});

test("a shadow summary parses", () => {
  const parsed = parseShadowSummary({
    verdict: "HELPS",
    suggested_count: 10,
    unsuggested_count: 8,
    suggested_win_rate: 0.7,
    unsuggested_win_rate: 0.4,
    suggested_interval: [0.5, 0.9],
    unsuggested_interval: [0.2, 0.6],
  });
  assert.equal(parsed.verdict, "HELPS");
  assert.equal(parsed.suggested_count, 10);
});

test("malformed opportunities are rejected", () => {
  assert.throws(() => parseOpportunityList({ opportunities: [{}] }));
  assert.throws(() =>
    parseOpportunityList({
      opportunities: [{ ...opportunity, probability: "high" }],
    }),
  );
  assert.throws(() =>
    parseShadowSummary({ verdict: "HELPS", suggested_count: -1 }),
  );
});
