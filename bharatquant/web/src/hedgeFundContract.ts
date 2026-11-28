import { isRecord } from "./contracts.ts";

export type AiHfPhase =
  | "queued"
  | "preparing"
  | "running"
  | "parsed"
  | "failed"
  | "cancelled"
  | "unknown";

export type AiHfModel = {
  display_name: string;
  model_name: string;
  provider: string;
  env_var: string | null;
  runnable: boolean;
};

export type AiHfStatus = {
  installed: boolean;
  version: string | null;
  active_provider: string | null;
  active_model: string | null;
  bq_provider: string;
  has_llm_key: boolean;
  has_financial_datasets_key: boolean;
  configured_vars: string[];
  missing_vars: string[];
  models: AiHfModel[];
  registry_error: string | null;
  can_run: boolean;
};

export type RiskLimit = {
  max_position_pct: number;
  max_gross_exposure: number;
  halve_drawdown_pct: number | null;
  max_drawdown_pct: number | null;
  max_net_exposure: number | null;
};

export type MandateSummary = {
  name: string;
  capital: number | null;
  rebalance: string;
  benchmark: string;
  has_universe_rule: boolean;
  strategies: { name: string; title: string; weight: number; mode: string; models: string[] }[];
  personas: string[];
  quant_models: string[];
  risk: RiskLimit;
};

export type Mandate = {
  name: string;
  yaml: string;
  valid: boolean;
  error: string | null;
  summary: MandateSummary | null;
  updated_at: string | null;
};

export type BacktestMetrics = {
  total_return_pct: number;
  annualized_return_pct: number;
  sharpe_ratio: number;
  max_drawdown_pct: number;
  benchmark_return_pct: number;
  excess_return_pct: number;
  n_cycles: number;
  n_orders: number;
  traded_notional: number;
  trading_cost: number;
};

export type BacktestSummary = {
  run_id: string;
  fund: string;
  universe: string[];
  start: string;
  end: string;
  benchmark: string;
  rebalance: string;
  capital: number;
  metrics: BacktestMetrics;
  updated_at: string | null;
};

export type Signal = {
  model_name: string;
  ticker: string;
  date: string;
  value: number;
  side: "long" | "short" | "neutral";
  reasoning: string | null;
};

export type Clamp = {
  limit: string;
  ticker: string | null;
  sector: string | null;
  before: number;
  after: number;
};

export type Execution = {
  as_of: string;
  orders: { ticker: string; side: string; quantity: number; price: number; notional: number }[];
  fills: { ticker: string; side: string; quantity: number; price: number }[];
  clamps: Clamp[];
  nav: number;
  cash: number;
  traded_notional: number;
};

export type Decision = {
  as_of: string;
  universe: string[];
  target_weights: Record<string, number>;
  final_weights: Record<string, number>;
  drawdown: number | null;
  is_stopped_out: boolean;
  is_halved: boolean;
  clamps: Clamp[];
  strategies: {
    name: string;
    slice: number;
    final_contribution: Record<string, number>;
    flat_reason: string | null;
    signals: Signal[];
  }[];
};

export type SessionRecord = {
  session: string;
  nav: number;
  cash: number;
  positions: Record<string, number>;
  marks: Record<string, number>;
  benchmark: string;
  benchmark_close: number;
  rebalance: boolean;
  hash: string;
  prev_hash: string | null;
  llm_model: string | null;
  executed: Execution | null;
  decision: Decision | null;
};

export type BacktestDetail = {
  summary: BacktestSummary;
  dates: string[];
  nav: number[];
  benchmark_nav: number[];
  trades: TradeRow[];
  sessions: SessionRecord[];
  session_total: number;
};

export type TradeRow = {
  signal_date: string;
  execution_date: string;
  ticker: string;
  side: string;
  quantity: number;
  price: number;
  reference_price: number;
  cost: number;
};

export type Fund = {
  name: string;
  mandate: string | null;
  universe: string[];
  capital: number | null;
  rebalance: string | null;
  benchmark: string | null;
  cadence_label: string | null;
  strategies: { name: string; title: string; weight: number }[];
  nav: number | null;
  return_pct: number | null;
  drawdown_pct: number | null;
  status: "ACTIVE" | "HALTED";
  halt_reason: string | null;
  halted_since: string | null;
  last_session: string | null;
  session_count: number;
  chain_verified: boolean;
  problem: string | null;
  created: string | null;
};

export type Preview = {
  preview_id: string;
  fund: string;
  session: string;
  head: string | null;
  nav: number;
  cash: number;
  rebalance: boolean;
  benchmark: string;
  benchmark_close: number;
  orders: { ticker: string; side: string; quantity: number; price: number; notional: number }[];
  fills: { ticker: string; side: string; quantity: number; price: number }[];
  risk_checks: (Clamp & { passed: boolean })[];
  reasoning: {
    strategy: string;
    model_name: string;
    ticker: string;
    value: number;
    side: string;
    reasoning: string | null;
  }[];
  positions: Record<string, number>;
  marks: Record<string, number>;
  stopped_out: boolean;
  halved: boolean;
};

export type Ledger = {
  fund: string;
  sessions: SessionRecord[];
  session_total: number;
  head: string | null;
  chain_verified: boolean;
  problem: string | null;
  verify_error: string | null;
  events: Record<string, unknown>[];
  control: { halted: boolean; reason: string | null; since: string | null };
};

export type Progress = {
  run_id: string;
  phase: AiHfPhase;
  detail: string;
  elapsed_seconds: number;
  lines: string[];
  error: string | null;
  done: boolean;
};

// ---------------------------------------------------------------------------
// parsers
//
// Every payload is checked field by field. A contract that quietly coerced a
// missing number to 0 would render a strategy as flat on a screen where the
// real answer is "we do not know", which is the one thing this tab must not do.
// ---------------------------------------------------------------------------

function text(value: unknown, field: string): string {
  if (typeof value !== "string") {
    throw new Error(`Invalid ${field}`);
  }
  return value;
}

function optionalText(value: unknown, field: string): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  return text(value, field);
}

function number(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`Invalid ${field}`);
  }
  return value;
}

function optionalNumber(value: unknown, field: string): number | null {
  if (value === null || value === undefined) {
    return null;
  }
  return number(value, field);
}

function bool(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`Invalid ${field}`);
  }
  return value;
}

function stringList(value: unknown, field: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`Invalid ${field}`);
  }
  return value as string[];
}

function numberMap(value: unknown, field: string): Record<string, number> {
  if (!isRecord(value)) {
    throw new Error(`Invalid ${field}`);
  }
  const out: Record<string, number> = {};
  for (const [key, item] of Object.entries(value)) {
    out[key] = number(item, field);
  }
  return out;
}

function intMap(value: unknown, field: string): Record<string, number> {
  return numberMap(value, field);
}

export function parseAiHfModel(value: unknown): AiHfModel {
  if (!isRecord(value)) {
    throw new Error("Invalid model");
  }
  return {
    display_name: text(value.display_name, "model display name"),
    model_name: text(value.model_name, "model name"),
    provider: text(value.provider, "model provider"),
    env_var: optionalText(value.env_var, "model env var"),
    runnable: bool(value.runnable, "model runnable"),
  };
}

export function parseAiHfStatus(value: unknown): AiHfStatus {
  if (!isRecord(value) || !Array.isArray(value.models)) {
    throw new Error("Invalid AI Hedge Fund status");
  }
  return {
    installed: bool(value.installed, "installed"),
    version: optionalText(value.version, "version"),
    active_provider: optionalText(value.active_provider, "active provider"),
    active_model: optionalText(value.active_model, "active model"),
    bq_provider: text(value.bq_provider, "bq provider"),
    has_llm_key: bool(value.has_llm_key, "has_llm_key"),
    has_financial_datasets_key: bool(value.has_financial_datasets_key, "has_financial_datasets_key"),
    configured_vars: stringList(value.configured_vars, "configured vars"),
    missing_vars: stringList(value.missing_vars, "missing vars"),
    models: value.models.map(parseAiHfModel),
    registry_error: optionalText(value.registry_error, "registry error"),
    can_run: bool(value.can_run, "can_run"),
  };
}

export function parseRiskLimit(value: unknown): RiskLimit {
  if (!isRecord(value)) {
    throw new Error("Invalid risk limits");
  }
  return {
    max_position_pct: number(value.max_position_pct, "max_position_pct"),
    max_gross_exposure: number(value.max_gross_exposure, "max_gross_exposure"),
    halve_drawdown_pct: optionalNumber(value.halve_drawdown_pct, "halve_drawdown_pct"),
    max_drawdown_pct: optionalNumber(value.max_drawdown_pct, "max_drawdown_pct"),
    max_net_exposure: optionalNumber(value.max_net_exposure, "max_net_exposure"),
  };
}

export function parseMandateSummary(value: unknown): MandateSummary {
  if (!isRecord(value) || !Array.isArray(value.strategies)) {
    throw new Error("Invalid mandate summary");
  }
  const personas: string[] = [];
  const quantModels: string[] = [];
  const strategies = value.strategies.map((entry) => {
    if (!isRecord(entry) || !Array.isArray(entry.models)) {
      throw new Error("Invalid mandate strategy");
    }
    const models = entry.models.map((model) => {
      if (!isRecord(model)) {
        throw new Error("Invalid mandate model");
      }
      const name = text(model.name, "model name");
      if (QUANT_IDS.includes(name)) {
        if (!quantModels.includes(name)) {
          quantModels.push(name);
        }
      } else if (PERSONA_IDS.includes(name) && !personas.includes(name)) {
        personas.push(name);
      }
      return name;
    });
    return {
      name: text(entry.name, "strategy name"),
      title: text(entry.title, "strategy title"),
      weight: number(entry.weight, "strategy weight"),
      mode: text(entry.mode, "strategy mode"),
      models,
    };
  });
  return {
    name: text(value.name, "mandate name"),
    capital: optionalNumber(value.capital, "capital"),
    rebalance: text(value.rebalance, "rebalance"),
    benchmark: text(value.benchmark, "benchmark"),
    has_universe_rule: bool(value.has_universe_rule, "has_universe_rule"),
    strategies,
    personas,
    quant_models: quantModels,
    risk: parseRiskLimit(value.risk),
  };
}

export function parseMandate(value: unknown): Mandate {
  if (!isRecord(value)) {
    throw new Error("Invalid mandate");
  }
  return {
    name: text(value.name, "mandate name"),
    yaml: text(value.yaml, "mandate yaml"),
    valid: bool(value.valid, "mandate validity"),
    error: optionalText(value.error, "mandate error"),
    summary: value.summary === null || value.summary === undefined ? null : parseMandateSummary(value.summary),
    updated_at: optionalText(value.updated_at, "updated_at"),
  };
}

export function parseMandateList(value: unknown): { mandates: Mandate[]; template: string } {
  if (!isRecord(value) || !Array.isArray(value.mandates)) {
    throw new Error("Invalid mandate list");
  }
  return {
    mandates: value.mandates.map(parseMandate),
    template: text(value.template, "mandate template"),
  };
}

export function parseBacktestMetrics(value: unknown): BacktestMetrics {
  if (!isRecord(value)) {
    throw new Error("Invalid backtest metrics");
  }
  return {
    total_return_pct: number(value.total_return_pct, "total return"),
    annualized_return_pct: number(value.annualized_return_pct, "annualized return"),
    sharpe_ratio: number(value.sharpe_ratio, "sharpe"),
    max_drawdown_pct: number(value.max_drawdown_pct, "max drawdown"),
    benchmark_return_pct: number(value.benchmark_return_pct, "benchmark return"),
    excess_return_pct: number(value.excess_return_pct, "excess return"),
    n_cycles: number(value.n_cycles, "cycles"),
    n_orders: number(value.n_orders, "orders"),
    traded_notional: number(value.traded_notional, "traded notional"),
    trading_cost: number(value.trading_cost, "trading cost"),
  };
}

export function parseBacktestSummary(value: unknown): BacktestSummary {
  if (!isRecord(value)) {
    throw new Error("Invalid backtest summary");
  }
  return {
    run_id: text(value.run_id, "run id"),
    fund: text(value.fund, "fund"),
    universe: stringList(value.universe, "universe"),
    start: text(value.start, "start"),
    end: text(value.end, "end"),
    benchmark: text(value.benchmark, "benchmark"),
    rebalance: text(value.rebalance, "rebalance"),
    capital: number(value.capital, "capital"),
    metrics: parseBacktestMetrics(value.metrics),
    updated_at: optionalText(value.updated_at, "updated_at"),
  };
}

export function parseBacktestList(value: unknown): BacktestSummary[] {
  if (!isRecord(value) || !Array.isArray(value.backtests)) {
    throw new Error("Invalid backtest list");
  }
  return value.backtests.map(parseBacktestSummary);
}

export function parseSignal(value: unknown): Signal {
  if (!isRecord(value)) {
    throw new Error("Invalid signal");
  }
  const side = text(value.side, "signal side");
  if (side !== "long" && side !== "short" && side !== "neutral") {
    throw new Error("Invalid signal side");
  }
  return {
    model_name: text(value.model_name, "signal model"),
    ticker: text(value.ticker, "signal ticker"),
    date: text(value.date, "signal date"),
    value: number(value.value, "signal value"),
    side,
    reasoning: optionalText(value.reasoning, "signal reasoning"),
  };
}

export function parseClamp(value: unknown): Clamp {
  if (!isRecord(value)) {
    throw new Error("Invalid clamp");
  }
  return {
    limit: text(value.limit, "clamp limit"),
    ticker: optionalText(value.ticker, "clamp ticker"),
    sector: optionalText(value.sector, "clamp sector"),
    before: number(value.before, "clamp before"),
    after: number(value.after, "clamp after"),
  };
}

function clampList(value: unknown): Clamp[] {
  if (value === null || value === undefined) {
    return [];
  }
  if (!Array.isArray(value)) {
    throw new Error("Invalid clamps");
  }
  return value.map(parseClamp);
}

export function parseSession(value: unknown): SessionRecord {
  if (!isRecord(value)) {
    throw new Error("Invalid session record");
  }
  return {
    session: text(value.session, "session"),
    nav: number(value.nav, "session nav"),
    cash: number(value.cash, "session cash"),
    positions: intMap(value.positions, "session positions"),
    marks: numberMap(value.marks, "session marks"),
    benchmark: text(value.benchmark, "session benchmark"),
    benchmark_close: number(value.benchmark_close, "benchmark close"),
    rebalance: bool(value.rebalance, "session rebalance"),
    hash: text(value.hash, "session hash"),
    prev_hash: optionalText(value.prev_hash, "session prev hash"),
    llm_model: optionalText(value.llm_model, "session llm model"),
    executed: value.executed === null || value.executed === undefined ? null : parseExecution(value.executed),
    decision: value.decision === null || value.decision === undefined ? null : parseDecision(value.decision),
  };
}

export function parseExecution(value: unknown): Execution {
  if (!isRecord(value) || !Array.isArray(value.orders) || !Array.isArray(value.fills)) {
    throw new Error("Invalid execution");
  }
  return {
    as_of: text(value.as_of, "execution as_of"),
    orders: value.orders.map((order) => {
      if (!isRecord(order)) {
        throw new Error("Invalid order");
      }
      return {
        ticker: text(order.ticker, "order ticker"),
        side: text(order.side, "order side"),
        quantity: number(order.quantity, "order quantity"),
        price: number(order.price, "order price"),
        notional: number(order.notional, "order notional"),
      };
    }),
    fills: value.fills.map((fill) => {
      if (!isRecord(fill)) {
        throw new Error("Invalid fill");
      }
      return {
        ticker: text(fill.ticker, "fill ticker"),
        side: text(fill.side, "fill side"),
        quantity: number(fill.quantity, "fill quantity"),
        price: number(fill.price, "fill price"),
      };
    }),
    clamps: clampList(value.clamps),
    nav: number(value.nav, "execution nav"),
    cash: number(value.cash, "execution cash"),
    traded_notional: number(value.traded_notional, "execution traded notional"),
  };
}

export function parseDecision(value: unknown): Decision {
  if (!isRecord(value) || !Array.isArray(value.strategies)) {
    throw new Error("Invalid decision");
  }
  return {
    as_of: text(value.as_of, "decision as_of"),
    universe: stringList(value.universe, "decision universe"),
    target_weights: numberMap(value.target_weights, "target weights"),
    final_weights: numberMap(value.final_weights, "final weights"),
    drawdown: optionalNumber(value.drawdown, "drawdown"),
    is_stopped_out: bool(value.is_stopped_out, "stopped out"),
    is_halved: bool(value.is_halved, "halved"),
    clamps: clampList(value.clamps),
    strategies: value.strategies.map((entry) => {
      if (!isRecord(entry) || !Array.isArray(entry.signals)) {
        throw new Error("Invalid decision strategy");
      }
      return {
        name: text(entry.name, "strategy name"),
        slice: number(entry.slice, "strategy slice"),
        final_contribution: numberMap(entry.final_contribution, "strategy contribution"),
        flat_reason: optionalText(entry.flat_reason, "strategy flat reason"),
        signals: entry.signals.map(parseSignal),
      };
    }),
  };
}

export function parseTradeRow(value: unknown): TradeRow {
  if (!isRecord(value)) {
    throw new Error("Invalid trade row");
  }
  return {
    signal_date: text(value.signal_date, "signal date"),
    execution_date: text(value.execution_date, "execution date"),
    ticker: text(value.ticker, "trade ticker"),
    side: text(value.side, "trade side"),
    quantity: number(value.quantity, "trade quantity"),
    price: number(value.price, "trade price"),
    reference_price: number(value.reference_price, "trade reference price"),
    cost: number(value.cost, "trade cost"),
  };
}

export function parseBacktestDetail(value: unknown): BacktestDetail {
  if (
    !isRecord(value) ||
    !Array.isArray(value.dates) ||
    !Array.isArray(value.trades) ||
    !Array.isArray(value.sessions)
  ) {
    throw new Error("Invalid backtest detail");
  }
  const nav = (value.nav as unknown[]).map((point) => number(point, "curve point"));
  const benchmark = (value.benchmark_nav as unknown[]).map((point) =>
    number(point, "benchmark point"),
  );
  if (nav.length !== value.dates.length || benchmark.length !== value.dates.length) {
    throw new Error("Invalid backtest detail");
  }
  return {
    summary: parseBacktestSummary(value.summary),
    dates: value.dates.map((item) => text(item, "curve date")),
    nav,
    benchmark_nav: benchmark,
    trades: value.trades.map(parseTradeRow),
    sessions: value.sessions.map(parseSession),
    session_total: number(value.session_total, "session total"),
  };
}

export function parseFund(value: unknown): Fund {
  if (!isRecord(value)) {
    throw new Error("Invalid paper fund");
  }
  const status = text(value.status, "fund status");
  if (status !== "ACTIVE" && status !== "HALTED") {
    throw new Error("Invalid fund status");
  }
  const strategies = Array.isArray(value.strategies)
    ? value.strategies.map((entry) => {
        if (!isRecord(entry)) {
          throw new Error("Invalid fund strategy");
        }
        return {
          name: text(entry.name, "fund strategy name"),
          title: text(entry.title, "fund strategy title"),
          weight: number(entry.weight, "fund strategy weight"),
        };
      })
    : [];
  return {
    name: text(value.name, "fund name"),
    mandate: optionalText(value.mandate, "fund mandate"),
    universe: stringList(value.universe, "fund universe"),
    capital: optionalNumber(value.capital, "fund capital"),
    rebalance: optionalText(value.rebalance, "fund rebalance"),
    benchmark: optionalText(value.benchmark, "fund benchmark"),
    cadence_label: optionalText(value.cadence_label, "fund cadence"),
    strategies,
    nav: optionalNumber(value.nav, "fund nav"),
    return_pct: optionalNumber(value.return_pct, "fund return"),
    drawdown_pct: optionalNumber(value.drawdown_pct, "fund drawdown"),
    status,
    halt_reason: optionalText(value.halt_reason, "halt reason"),
    halted_since: optionalText(value.halted_since, "halted since"),
    last_session: optionalText(value.last_session, "last session"),
    session_count: number(value.session_count, "session count"),
    chain_verified: bool(value.chain_verified, "chain verified"),
    problem: optionalText(value.problem, "fund problem"),
    created: optionalText(value.created, "fund created"),
  };
}

export function parseFundList(value: unknown): Fund[] {
  if (!isRecord(value) || !Array.isArray(value.funds)) {
    throw new Error("Invalid paper fund list");
  }
  return value.funds.map(parseFund);
}

export function parsePreview(value: unknown): Preview {
  if (
    !isRecord(value) ||
    !Array.isArray(value.orders) ||
    !Array.isArray(value.fills) ||
    !Array.isArray(value.risk_checks) ||
    !Array.isArray(value.reasoning)
  ) {
    throw new Error("Invalid session preview");
  }
  return {
    preview_id: text(value.preview_id, "preview id"),
    fund: text(value.fund, "preview fund"),
    session: text(value.session, "preview session"),
    head: optionalText(value.head, "preview head"),
    nav: number(value.nav, "preview nav"),
    cash: number(value.cash, "preview cash"),
    rebalance: bool(value.rebalance, "preview rebalance"),
    benchmark: text(value.benchmark, "preview benchmark"),
    benchmark_close: number(value.benchmark_close, "preview benchmark close"),
    orders: value.orders.map((order) => {
      if (!isRecord(order)) {
        throw new Error("Invalid preview order");
      }
      return {
        ticker: text(order.ticker, "order ticker"),
        side: text(order.side, "order side"),
        quantity: number(order.quantity, "order quantity"),
        price: number(order.price, "order price"),
        notional: number(order.notional, "order notional"),
      };
    }),
    fills: value.fills.map((fill) => {
      if (!isRecord(fill)) {
        throw new Error("Invalid preview fill");
      }
      return {
        ticker: text(fill.ticker, "fill ticker"),
        side: text(fill.side, "fill side"),
        quantity: number(fill.quantity, "fill quantity"),
        price: number(fill.price, "fill price"),
      };
    }),
    risk_checks: value.risk_checks.map((check) => {
      if (!isRecord(check)) {
        throw new Error("Invalid risk check");
      }
      return { ...parseClamp(check), passed: bool(check.passed, "risk check passed") };
    }),
    reasoning: value.reasoning.map((entry) => {
      if (!isRecord(entry)) {
        throw new Error("Invalid reasoning");
      }
      return {
        strategy: text(entry.strategy, "reasoning strategy"),
        model_name: text(entry.model_name, "reasoning model"),
        ticker: text(entry.ticker, "reasoning ticker"),
        value: number(entry.value, "reasoning value"),
        side: text(entry.side, "reasoning side"),
        reasoning: optionalText(entry.reasoning, "reasoning text"),
      };
    }),
    positions: intMap(value.positions, "preview positions"),
    marks: numberMap(value.marks, "preview marks"),
    stopped_out: bool(value.stopped_out, "preview stopped out"),
    halved: bool(value.halved, "preview halved"),
  };
}

export function parseLedger(value: unknown): Ledger {
  if (!isRecord(value) || !Array.isArray(value.sessions) || !Array.isArray(value.events)) {
    throw new Error("Invalid ledger");
  }
  if (!isRecord(value.control)) {
    throw new Error("Invalid ledger control");
  }
  return {
    fund: text(value.fund, "ledger fund"),
    sessions: value.sessions.map(parseSession),
    session_total: number(value.session_total, "ledger session total"),
    head: optionalText(value.head, "ledger head"),
    chain_verified: bool(value.chain_verified, "ledger chain verified"),
    problem: optionalText(value.problem, "ledger problem"),
    verify_error: optionalText(value.verify_error, "ledger verify error"),
    events: value.events.map((event) => {
      if (!isRecord(event)) {
        throw new Error("Invalid ledger event");
      }
      return event;
    }),
    control: {
      halted: bool(value.control.halted, "control halted"),
      reason: optionalText(value.control.reason, "control reason"),
      since: optionalText(value.control.since, "control since"),
    },
  };
}

export function parseProgress(value: unknown): Progress {
  if (!isRecord(value)) {
    throw new Error("Invalid progress");
  }
  const phase = text(value.phase, "phase") as AiHfPhase;
  return {
    run_id: text(value.run_id, "run id"),
    phase: PHASES.includes(phase) ? phase : "unknown",
    detail: text(value.detail ?? "", "detail"),
    elapsed_seconds: number(value.elapsed_seconds ?? 0, "elapsed"),
    lines: stringList(value.lines ?? [], "lines"),
    error: optionalText(value.error, "error"),
    done: bool(value.done ?? false, "done"),
  };
}

export function parseRunProgress(value: unknown): { run_id: string; running: boolean; progress: Progress | null } {
  if (!isRecord(value)) {
    throw new Error("Invalid run progress");
  }
  return {
    run_id: text(value.run_id, "run id"),
    running: bool(value.running, "running"),
    progress: value.progress === null || value.progress === undefined ? null : parseProgress(value.progress),
  };
}

export function parseDataKey(value: unknown): { configured: boolean; masked: string | null } {
  if (!isRecord(value) || !isRecord(value.financial_datasets)) {
    throw new Error("Invalid AI Hedge Fund settings");
  }
  const entry = value.financial_datasets;
  return {
    configured: bool(entry.configured, "data key configured"),
    masked: optionalText(entry.masked, "data key masked"),
  };
}

// ---------------------------------------------------------------------------
// what aihf registers, and formatting
//
// Copied deliberately from aihf's own registry (hedge_fund/signals) because the
// mandate form has to offer exactly the models aihf can construct.
// ---------------------------------------------------------------------------

export const PERSONAS = ["buffett", "munger", "graham", "lynch", "druckenmiller"] as const;
export const QUANT_MODELS = ["pead"] as const;
// Widened copies: `.includes` on a readonly tuple narrows its argument to the
// literal union, which a name read from a payload is not.
const PERSONA_IDS: readonly string[] = PERSONAS;
const QUANT_IDS: readonly string[] = QUANT_MODELS;
export const PERSONA_LABELS: Record<string, string> = {
  buffett: "Buffett",
  munger: "Munger",
  graham: "Graham",
  lynch: "Lynch",
  druckenmiller: "Druckenmiller",
  pead: "Earnings Drift (PEAD)",
};

export const REBALANCE_CADENCES = ["daily", "weekly", "monthly"] as const;
export const PHASES: AiHfPhase[] = [
  "queued",
  "preparing",
  "running",
  "parsed",
  "failed",
  "cancelled",
  "unknown",
];

export function formatPct(value: number | null, digits = 2): string {
  if (value === null) {
    return "—";
  }
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatMoney(value: number | null): string {
  if (value === null) {
    return "—";
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export function formatNumber(value: number | null, digits = 2): string {
  if (value === null) {
    return "—";
  }
  return value.toFixed(digits);
}

export function phaseTone(phase: AiHfPhase): "ok" | "warn" | "danger" | "muted" {
  if (phase === "parsed") {
    return "ok";
  }
  if (phase === "queued" || phase === "preparing" || phase === "running") {
    return "warn";
  }
  if (phase === "failed" || phase === "cancelled") {
    return "danger";
  }
  return "muted";
}

export function chainTone(verified: boolean, problem: string | null): "ok" | "danger" | "muted" {
  if (problem) {
    return "danger";
  }
  return verified ? "ok" : "muted";
}

export function statusTone(status: "ACTIVE" | "HALTED"): "ok" | "danger" {
  return status === "ACTIVE" ? "ok" : "danger";
}

export function clampLabel(limit: string): string {
  const labels: Record<string, string> = {
    max_position_pct: "Single-stock cap",
    max_gross_exposure: "Gross exposure cap",
    max_net_exposure: "Net exposure band",
    max_drawdown_pct: "Drawdown stop-out",
    halve_drawdown_pct: "Drawdown halving",
    max_adv_pct: "Daily volume cap",
    max_beta: "Beta cap",
    max_sector_pct: "Sector cap",
    max_volatility: "Volatility cap",
  };
  return labels[limit] ?? limit;
}
