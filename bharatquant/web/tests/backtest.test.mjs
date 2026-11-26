import assert from "node:assert/strict";
import test from "node:test";

import { parseBacktest, parseBacktests, parseStrategies } from "../src/backtestContract.ts";

const strategy = {
  strategy_id: "seed-momentum-20",
  version: "1",
  name: "Momentum 20",
  kind: "RULE",
  lifecycle: "DRAFT",
  definition: { lookback: 20 },
  content_hash: "abc",
  is_seed: true,
  created_at: "2026-11-26T10:00:00Z",
};

const run = {
  run_id: "backtest_1",
  strategy_id: "seed-momentum-20",
  version: "1",
  engine: "FAST",
  start: "2026-01-01T00:00:00Z",
  end: "2026-03-01T00:00:00Z",
  initial_capital_paise: "10000000",
  metrics: {
    trades: 2,
    wins: 1,
    losses: 1,
    gross_pnl_paise: "1000",
    charges_paise: "120",
    net_pnl_paise: "880",
    final_capital_paise: "10000880",
    return_bps: 8,
    win_rate: 0.5,
    max_drawdown_paise: "200",
  },
  trades: [
    {
      symbol: "NSE:RELIANCE",
      entry_at: "2026-01-05T04:00:00Z",
      exit_at: "2026-01-10T04:00:00Z",
      entry_price_paise: "10000",
      exit_price_paise: "10100",
      quantity: 10,
      gross_pnl_paise: "1000",
      charges_paise: "120",
      net_pnl_paise: "880",
      exit_reason: "TARGET",
    },
  ],
  content_hash: "def",
  created_at: "2026-11-26T10:00:00Z",
  review_decision: null,
  review_note: null,
};

test("strategy records parse", () => {
  const parsed = parseStrategies({ strategies: [strategy] });
  assert.equal(parsed[0].is_seed, true);
  assert.equal(parsed[0].definition.lookback, 20);
});

test("backtest runs and metrics parse", () => {
  const parsed = parseBacktest(run);
  assert.equal(parsed.metrics.trades, 2);
  assert.equal(parsed.trades[0].exit_reason, "TARGET");
  assert.equal(parsed.review_decision, null);
  assert.equal(parseBacktests({ runs: [run] })[0].run_id, "backtest_1");
});

test("malformed backtests are rejected", () => {
  assert.throws(() => parseBacktest({ ...run, engine: "MAYBE" }));
  assert.throws(() =>
    parseBacktest({ ...run, metrics: { ...run.metrics, trades: -1 } }),
  );
});
