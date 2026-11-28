import assert from "node:assert/strict";
import test from "node:test";

import {
  PERSONAS,
  QUANT_MODELS,
  chainTone,
  clampLabel,
  formatMoney,
  formatNumber,
  formatPct,
  parseAiHfStatus,
  parseBacktestDetail,
  parseBacktestList,
  parseDataKey,
  parseFund,
  parseFundList,
  parseLedger,
  parseMandate,
  parseMandateList,
  parsePreview,
  parseProgress,
  parseRunProgress,
  phaseTone,
  statusTone,
} from "../src/hedgeFundContract.ts";

const validStatus = {
  installed: true,
  version: "2.7.2",
  active_provider: "Google",
  active_model: null,
  bq_provider: "GEMINI",
  has_llm_key: true,
  has_financial_datasets_key: true,
  configured_vars: ["FINANCIAL_DATASETS_API_KEY", "GOOGLE_API_KEY"],
  missing_vars: ["ANTHROPIC_API_KEY"],
  models: [
    {
      display_name: "Gemini 3.8 Flash",
      model_name: "gemini-3.8-flash",
      provider: "Google",
      env_var: "GOOGLE_API_KEY",
      runnable: true,
    },
  ],
  registry_error: null,
  can_run: true,
};

const validMandateSummary = {
  name: "example-fund",
  capital: 100000,
  rebalance: "weekly",
  benchmark: "SPY",
  has_universe_rule: false,
  strategies: [
    {
      name: "deep-value",
      title: "Deep Value",
      weight: 0.6,
      mode: "long_only",
      // The backend sends `summary.strategies[].models` as {name, weight}
      // objects (aihf's ModelSpec), not bare names.
      models: [{ name: "graham", weight: 2 }, { name: "buffett", weight: 1 }, { name: "munger", weight: 1 }],
    },
    {
      name: "earnings-drift",
      title: "Earnings Drift",
      weight: 0.4,
      mode: "long_short",
      models: [{ name: "pead", weight: 1 }],
    },
  ],
  personas: ["graham", "buffett", "munger"],
  quant_models: ["pead"],
  risk: {
    max_position_pct: 0.25,
    max_gross_exposure: 1.0,
    halve_drawdown_pct: 0.1,
    max_drawdown_pct: 0.2,
    max_net_exposure: null,
  },
};

const validMandate = {
  name: "example-fund",
  yaml: "schema_version: 2\n",
  valid: true,
  error: null,
  summary: validMandateSummary,
  updated_at: "2026-11-28T10:00:00+00:00",
};

const validFund = {
  name: "alpha",
  mandate: "example-fund",
  universe: ["AAPL", "MSFT"],
  capital: 100000,
  rebalance: "weekly",
  benchmark: "SPY",
  cadence_label: "weekly",
  strategies: [{ name: "deep-value", title: "Deep Value", weight: 0.6 }],
  nav: null,
  return_pct: null,
  drawdown_pct: null,
  status: "ACTIVE",
  halt_reason: null,
  halted_since: null,
  last_session: null,
  session_count: 0,
  chain_verified: true,
  problem: null,
  created: "2026-11-28",
};

const validSession = {
  session: "2025-01-03",
  nav: 101000,
  cash: 20000,
  positions: { AAPL: 100 },
  marks: { AAPL: 810 },
  benchmark: "SPY",
  benchmark_close: 500,
  rebalance: true,
  hash: "b".repeat(64),
  prev_hash: null,
  llm_model: "claude-opus-5-5",
  executed: null,
  decision: {
    as_of: "2025-01-03",
    universe: ["AAPL"],
    target_weights: { AAPL: 0.25 },
    final_weights: { AAPL: 0.25 },
    drawdown: 0.02,
    is_stopped_out: false,
    is_halved: false,
    clamps: [{ limit: "max_position_pct", ticker: "AAPL", sector: null, before: 0.4, after: 0.25 }],
    strategies: [
      {
        name: "deep-value",
        slice: 0.6,
        final_contribution: { AAPL: 0.15 },
        flat_reason: null,
        signals: [],
      },
    ],
  },
};

const validDetail = {
  summary: {
    run_id: "example-fund-2025-01-02-2025-01-03",
    fund: "example-fund",
    universe: ["AAPL"],
    start: "2025-01-02",
    end: "2025-01-03",
    benchmark: "SPY",
    rebalance: "weekly",
    capital: 100000,
    metrics: {
      total_return_pct: 0.01,
      annualized_return_pct: 0.12,
      sharpe_ratio: 1.5,
      max_drawdown_pct: 0.02,
      benchmark_return_pct: 0.005,
      excess_return_pct: 0.005,
      n_cycles: 1,
      n_orders: 2,
      traded_notional: 40000,
      trading_cost: 12.5,
    },
    updated_at: null,
  },
  dates: ["2025-01-02", "2025-01-03"],
  nav: [100000, 101000],
  benchmark_nav: [100000, 100500],
  trades: [
    {
      signal_date: "2025-01-03",
      execution_date: "2025-01-04",
      ticker: "AAPL",
      side: "buy",
      quantity: 5,
      price: 804,
      reference_price: 800,
      cost: 20,
    },
  ],
  sessions: [validSession],
  session_total: 1,
};

const validPreview = {
  preview_id: "abc123.0",
  fund: "alpha",
  session: "2025-01-03",
  head: null,
  nav: 100000,
  cash: 50000,
  rebalance: true,
  benchmark: "SPY",
  benchmark_close: 500,
  orders: [{ ticker: "AAPL", side: "buy", quantity: 10, price: 800, notional: 8000 }],
  fills: [{ ticker: "AAPL", side: "buy", quantity: 10, price: 805 }],
  risk_checks: [
    { limit: "max_position_pct", ticker: "AAPL", sector: null, before: 0.4, after: 0.25, passed: true },
  ],
  reasoning: [
    {
      strategy: "deep-value",
      model_name: "buffett",
      ticker: "AAPL",
      value: 0.7,
      side: "long",
      reasoning: "durable moat",
    },
  ],
  positions: { AAPL: 10 },
  marks: { AAPL: 810 },
  stopped_out: false,
  halved: false,
};

const validProgress = {
  run_id: "example-fund-2025-01-02-2025-01-03",
  phase: "running",
  detail: "backtest",
  elapsed_seconds: 12.5,
  lines: ["one"],
  error: null,
  done: false,
};

// ---------------------------------------------------------------------------
// accepting valid payloads
// ---------------------------------------------------------------------------

test("accepts a valid AI Hedge Fund status", () => {
  const status = parseAiHfStatus(validStatus);
  assert.equal(status.installed, true);
  assert.equal(status.active_provider, "Google");
  assert.equal(status.active_model, null);
  assert.equal(status.can_run, true);
  assert.equal(status.models.length, 1);
  assert.equal(status.models[0].runnable, true);
});

test("accepts a mandate list with a template", () => {
  const parsed = parseMandateList({ mandates: [validMandate], template: "{}" });
  assert.equal(parsed.mandates[0].name, "example-fund");
  assert.equal(parsed.template, "{}");
  assert.deepEqual(parsed.mandates[0].summary?.quant_models, ["pead"]);
});

test("accepts a mandate with no summary yet", () => {
  const parsed = parseMandate({ ...validMandate, valid: false, summary: null, error: "bad" });
  assert.equal(parsed.valid, false);
  assert.equal(parsed.summary, null);
  assert.equal(parsed.error, "bad");
});

test("accepts a backtest detail and keeps the curve length", () => {
  const detail = parseBacktestDetail(validDetail);
  assert.equal(detail.nav.length, detail.dates.length);
  assert.equal(detail.benchmark_nav.length, detail.dates.length);
  assert.equal(detail.trades[0].signal_date, "2025-01-03");
  assert.equal(detail.sessions[0].decision?.clamps[0].limit, "max_position_pct");
});

test("accepts a saved backtest list", () => {
  const parsed = parseBacktestList({ backtests: [validDetail.summary] });
  assert.equal(parsed.length, 1);
  assert.equal(parsed[0].metrics.n_cycles, 1);
});

test("accepts a paper fund and list", () => {
  const fund = parseFund(validFund);
  assert.equal(fund.status, "ACTIVE");
  assert.equal(fund.nav, null);
  assert.equal(parseFundList({ funds: [validFund] }).length, 1);
});

test("accepts a ledger and a preview", () => {
  const ledger = parseLedger({
    fund: "alpha",
    sessions: [validSession],
    session_total: 1,
    head: "b".repeat(64),
    chain_verified: true,
    problem: null,
    verify_error: null,
    events: [{ kind: "tick" }],
    control: { halted: false, reason: null, since: null },
  });
  assert.equal(ledger.chain_verified, true);
  assert.equal(ledger.sessions.length, 1);
  assert.equal(ledger.events.length, 1);

  const preview = parsePreview(validPreview);
  assert.equal(preview.orders.length, 1);
  assert.equal(preview.risk_checks[0].passed, true);
  assert.equal(preview.reasoning[0].value, 0.7);
});

test("accepts progress and the run wrapper", () => {
  assert.equal(parseProgress(validProgress).phase, "running");
  const run = parseRunProgress({ run_id: "r", running: true, progress: validProgress });
  assert.equal(run.running, true);
  assert.equal(run.progress?.phase, "running");
  assert.equal(parseRunProgress({ run_id: "r", running: false, progress: null }).progress, null);
});

test("accepts the masked data key projection", () => {
  assert.deepEqual(parseDataKey({ financial_datasets: { configured: true, masked: "********1234" } }), {
    configured: true,
    masked: "********1234",
  });
  assert.deepEqual(parseDataKey({ financial_datasets: { configured: false, masked: null } }), {
    configured: false,
    masked: null,
  });
});

// ---------------------------------------------------------------------------
// rejecting malformed payloads
// ---------------------------------------------------------------------------

test("rejects a status with an unknown model shape", () => {
  const broken = { ...validStatus, models: [{}] };
  assert.throws(() => parseAiHfStatus(broken), /Invalid model/);
});

test("rejects a status missing can_run", () => {
  const broken = { ...validStatus };
  delete broken.can_run;
  assert.throws(() => parseAiHfStatus(broken), /Invalid can_run/);
});

test("rejects a status whose models is not an array", () => {
  assert.throws(() => parseAiHfStatus({ ...validStatus, models: null }), /Invalid AI Hedge Fund status/);
});

test("rejects a record with a non numeric nav", () => {
  const broken = {
    ...validDetail,
    sessions: [{ ...validSession, nav: "much" }],
  };
  assert.throws(() => parseBacktestDetail(broken), /Invalid session nav/);
});

test("rejects a curve whose series disagree in length", () => {
  const broken = { ...validDetail, nav: [100000] };
  assert.throws(() => parseBacktestDetail(broken), /Invalid backtest detail/);
});

test("rejects a decision with an unknown side", () => {
  const broken = {
    ...validDetail,
    sessions: [
      {
        ...validSession,
        decision: {
          ...validSession.decision,
          strategies: [
            {
              name: "deep-value",
              slice: 0.6,
              final_contribution: {},
              flat_reason: null,
              signals: [
                { model_name: "buffett", ticker: "AAPL", date: "2025-01-03", value: 0.5, side: "bullish" },
              ],
            },
          ],
        },
      },
    ],
  };
  assert.throws(() => parseBacktestDetail(broken), /Invalid signal side/);
});

test("rejects a fund with an unknown status", () => {
  assert.throws(() => parseFund({ ...validFund, status: "PAUSED" }), /Invalid fund status/);
});

test("rejects a preview whose risk check lacks passed", () => {
  const broken = {
    ...validPreview,
    risk_checks: [{ limit: "max_beta", ticker: null, sector: null, before: 1, after: 1 }],
  };
  assert.throws(() => parsePreview(broken), /Invalid risk check passed/);
});

test("rejects a ledger without a control block", () => {
  assert.throws(
    () =>
      parseLedger({
        fund: "alpha",
        sessions: [],
        session_total: 0,
        head: null,
        chain_verified: true,
        problem: null,
        verify_error: null,
        events: [],
      }),
    /Invalid ledger control/,
  );
});

test("rejects a mandate summary without risk limits", () => {
  const broken = { ...validMandate, summary: { ...validMandateSummary, risk: null } };
  assert.throws(() => parseMandate(broken), /Invalid risk limits/);
});

test("exposes exactly the models aihf registers", () => {
  assert.deepEqual([...PERSONAS], ["buffett", "munger", "graham", "lynch", "druckenmiller"]);
  assert.deepEqual([...QUANT_MODELS], ["pead"]);
});

// ---------------------------------------------------------------------------
// formatting
// ---------------------------------------------------------------------------

test("formats percentages from aihf's fractions", () => {
  assert.equal(formatPct(0.0123), "1.23%");
  assert.equal(formatPct(-0.5, 0), "-50%");
  assert.equal(formatPct(null), "—");
});

test("formats money and numbers without inventing a value", () => {
  assert.equal(formatMoney(null), "—");
  assert.equal(formatNumber(null), "—");
  assert.equal(formatNumber(1.2345), "1.23");
  assert.match(formatMoney(1234.5), /1,?234/);
});

test("points each phase at a badge tone", () => {
  assert.equal(phaseTone("parsed"), "ok");
  assert.equal(phaseTone("running"), "warn");
  assert.equal(phaseTone("failed"), "danger");
  assert.equal(phaseTone("unknown"), "muted");
});

test("a ledger problem outranks an unverified chain", () => {
  assert.equal(chainTone(true, null), "ok");
  assert.equal(chainTone(false, null), "muted");
  assert.equal(chainTone(true, "chain broken at session X"), "danger");
  assert.equal(statusTone("ACTIVE"), "ok");
  assert.equal(statusTone("HALTED"), "danger");
});

test("names every risk limit aihf can fire", () => {
  for (const limit of [
    "max_position_pct",
    "max_gross_exposure",
    "max_net_exposure",
    "max_drawdown_pct",
    "halve_drawdown_pct",
    "max_adv_pct",
    "max_beta",
    "max_sector_pct",
    "max_volatility",
  ]) {
    assert.notEqual(clampLabel(limit), limit, `${limit} needs a human label`);
  }
  assert.equal(clampLabel("something_new"), "something_new");
});
