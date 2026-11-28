import assert from "node:assert/strict";
import test from "node:test";

import { parseStrategyRun } from "../src/strategyContract.ts";

const plan = {
  signal: "BUY",
  symbol: "NSE:RELIANCE",
  exchange: "NSE",
  product: "CNC",
  order_type: "LIMIT",
  entry_price: "1351.00",
  stop_loss: "1306.47",
  target_price: "1417.80",
  reward_risk: "1.50",
  suggested_quantity: 10,
  confidence: 0.6,
  timeframe: "swing",
  invalidation_reason: "close below support",
  data_freshness: "delayed_yfinance",
  as_of: "2026-11-26T09:30:00+00:00",
};

const run = {
  strategy: "Buy the strongest momentum name.",
  timeframe: "swing",
  as_of: "2026-11-26T12:00:00+00:00",
  spec: {
    strategy: "Buy the strongest momentum name.",
    timeframe: "swing",
    symbols: ["NSE:RELIANCE", "NSE:TCS"],
    max_positions: 1,
    equity_paise: 1000000,
    available_cash_paise: 500000,
    risk_basis_points: 100,
    execution: "PAPER_RESEARCH",
  },
  symbols: [
    {
      requested: "NSE:RELIANCE",
      symbol: "NSE:RELIANCE",
      ok: true,
      error: null,
      signal: "BUY",
      composite: 0.4,
      mode: "RULES_ONLY",
      data_freshness: "delayed_yfinance",
      plan,
      agents: [
        { node: "technical", role: "Technical analyst", status: "ok", summary: "4/4" },
      ],
      facts: ["Trend (+0.4): above sma_50"],
      vetoes: [],
      notes: [],
    },
    {
      requested: "NSE:TCS",
      symbol: "NSE:TCS",
      ok: false,
      error: "No market data.",
    },
  ],
  decision: {
    strategy_summary: "One momentum candidate.",
    ranked_actions: [
      {
        symbol: "NSE:RELIANCE",
        action: "BUY",
        rank: 1,
        size_scale: 1.0,
        rationale: "Best composite.",
      },
    ],
    portfolio_notes: "One position.",
    risk_notes: "Long-only.",
    source: "rules",
    model: null,
    fallback_used: true,
    fallback_from: "reasoning",
  },
  accepted: [
    {
      symbol: "NSE:RELIANCE",
      rank: 1,
      action: "BUY",
      size_scale: 1.0,
      rationale: "Deterministic BUY plan.",
      notional_paise: 1351000,
      plan,
    },
  ],
  rejected: [
    { symbol: "NSE:TCS", action: "HOLD", reason: "No deterministic long plan." },
  ],
  notes: ["Paper research only."],
};

test("a valid strategy run is accepted", () => {
  const parsed = parseStrategyRun(run);
  assert.equal(parsed.symbols.length, 2);
  assert.equal(parsed.symbols[0].plan?.suggested_quantity, 10);
  assert.equal(parsed.symbols[1].ok, false);
  assert.equal(parsed.accepted[0].plan.entry_price, "1351.00");
  assert.equal(parsed.decision.ranked_actions[0].action, "BUY");
});

test("malformed strategy runs are rejected", () => {
  for (const override of [
    { symbols: [] },
    { decision: { ...run.decision, ranked_actions: [{ ...run.decision.ranked_actions[0], action: "MOON" }] } },
    { accepted: [{ ...run.accepted[0], plan: { ...plan, order_type: "MARKET" } }] },
    { spec: { ...run.spec, max_positions: -1 } },
    { symbols: [{ ...run.symbols[1], error: "" }] },
  ]) {
    assert.throws(() => parseStrategyRun({ ...run, ...override }));
  }
});

test("a full pipeline trace longer than 12 steps is accepted", () => {
  const nodes = [
    "gather_data",
    "news_context",
    "market_regime",
    "fundamental_events",
    "evidence_verifier",
    "technical",
    "bull",
    "bear",
    "judge",
    "risk_team",
    "size",
    "decision",
    "final_gate",
    "confidence",
  ];
  const agents = nodes.map((node) => ({
    node,
    role: node,
    status: "ok",
    summary: "ran",
  }));
  const parsed = parseStrategyRun({
    ...run,
    symbols: [{ ...run.symbols[0], agents }],
  });
  assert.equal(parsed.symbols[0].agents.length, 14);
});

test("the selected playbook is parsed when present", () => {
  assert.equal(parseStrategyRun(run).spec.playbook, null);
  const parsed = parseStrategyRun({
    ...run,
    spec: { ...run.spec, playbook: "defensive" },
  });
  assert.equal(parsed.spec.playbook, "defensive");
});
