import { isCount, isRecord, isTimestamp } from "./contracts.ts";

export type StrategyRecord = {
  strategy_id: string;
  version: string;
  name: string;
  kind: string;
  lifecycle: string;
  definition: Record<string, unknown>;
  content_hash: string;
  is_seed: boolean;
  created_at: string;
};

export type BacktestMetrics = {
  trades: number;
  wins: number;
  losses: number;
  gross_pnl_paise: string;
  charges_paise: string;
  net_pnl_paise: string;
  final_capital_paise: string;
  return_bps: number;
  win_rate: number | null;
  max_drawdown_paise: string;
};

export type BacktestTrade = {
  symbol: string;
  entry_at: string;
  exit_at: string;
  entry_price_paise: string;
  exit_price_paise: string;
  quantity: number;
  gross_pnl_paise: string;
  charges_paise: string;
  net_pnl_paise: string;
  exit_reason: string;
};

export type BacktestRun = {
  run_id: string;
  strategy_id: string;
  version: string;
  engine: "FAST" | "EVENT";
  start: string;
  end: string;
  initial_capital_paise: string;
  metrics: BacktestMetrics;
  trades: BacktestTrade[];
  content_hash: string;
  created_at: string;
  review_decision: string | null;
  review_note: string | null;
};

function signedPaise(value: unknown): string {
  if (typeof value !== "string" || !/^-?\d+$/.test(value)) {
    throw new Error("Invalid signed paise value");
  }
  return value;
}

function parseStrategy(value: unknown): StrategyRecord {
  if (
    !isRecord(value) ||
    typeof value.strategy_id !== "string" ||
    typeof value.version !== "string" ||
    typeof value.name !== "string" ||
    typeof value.kind !== "string" ||
    typeof value.lifecycle !== "string" ||
    !isRecord(value.definition) ||
    typeof value.content_hash !== "string" ||
    typeof value.is_seed !== "boolean" ||
    !isTimestamp(value.created_at)
  ) {
    throw new Error("Invalid strategy record");
  }
  return {
    strategy_id: value.strategy_id,
    version: value.version,
    name: value.name,
    kind: value.kind,
    lifecycle: value.lifecycle,
    definition: value.definition,
    content_hash: value.content_hash,
    is_seed: value.is_seed,
    created_at: value.created_at,
  };
}

export function parseStrategies(value: unknown): StrategyRecord[] {
  if (!isRecord(value) || !Array.isArray(value.strategies)) {
    throw new Error("Invalid strategy list");
  }
  return value.strategies.map(parseStrategy);
}

function parseMetrics(value: unknown): BacktestMetrics {
  if (
    !isRecord(value) ||
    !isCount(value.trades) ||
    !isCount(value.wins) ||
    !isCount(value.losses) ||
    typeof value.return_bps !== "number" ||
    (value.win_rate !== null && typeof value.win_rate !== "number")
  ) {
    throw new Error("Invalid backtest metrics");
  }
  return {
    trades: value.trades,
    wins: value.wins,
    losses: value.losses,
    gross_pnl_paise: signedPaise(value.gross_pnl_paise),
    charges_paise: signedPaise(value.charges_paise),
    net_pnl_paise: signedPaise(value.net_pnl_paise),
    final_capital_paise: signedPaise(value.final_capital_paise),
    return_bps: value.return_bps,
    win_rate: value.win_rate,
    max_drawdown_paise: signedPaise(value.max_drawdown_paise),
  };
}

function parseTrade(value: unknown): BacktestTrade {
  if (
    !isRecord(value) ||
    typeof value.symbol !== "string" ||
    !isTimestamp(value.entry_at) ||
    !isTimestamp(value.exit_at) ||
    !isCount(value.quantity) ||
    typeof value.exit_reason !== "string"
  ) {
    throw new Error("Invalid backtest trade");
  }
  return {
    symbol: value.symbol,
    entry_at: value.entry_at,
    exit_at: value.exit_at,
    entry_price_paise: signedPaise(value.entry_price_paise),
    exit_price_paise: signedPaise(value.exit_price_paise),
    quantity: value.quantity,
    gross_pnl_paise: signedPaise(value.gross_pnl_paise),
    charges_paise: signedPaise(value.charges_paise),
    net_pnl_paise: signedPaise(value.net_pnl_paise),
    exit_reason: value.exit_reason,
  };
}

export function parseBacktest(value: unknown): BacktestRun {
  if (
    !isRecord(value) ||
    typeof value.run_id !== "string" ||
    typeof value.strategy_id !== "string" ||
    typeof value.version !== "string" ||
    (value.engine !== "FAST" && value.engine !== "EVENT") ||
    !isTimestamp(value.start) ||
    !isTimestamp(value.end) ||
    typeof value.content_hash !== "string" ||
    !isTimestamp(value.created_at) ||
    (value.review_decision !== null && typeof value.review_decision !== "string") ||
    (value.review_note !== null && typeof value.review_note !== "string") ||
    !Array.isArray(value.trades)
  ) {
    throw new Error("Invalid backtest run");
  }
  return {
    run_id: value.run_id,
    strategy_id: value.strategy_id,
    version: value.version,
    engine: value.engine,
    start: value.start,
    end: value.end,
    initial_capital_paise: signedPaise(value.initial_capital_paise),
    metrics: parseMetrics(value.metrics),
    trades: value.trades.map(parseTrade),
    content_hash: value.content_hash,
    created_at: value.created_at,
    review_decision: value.review_decision,
    review_note: value.review_note,
  };
}

export function parseBacktests(value: unknown): BacktestRun[] {
  if (!isRecord(value) || !Array.isArray(value.runs)) {
    throw new Error("Invalid backtest list");
  }
  return value.runs.map(parseBacktest);
}
