import { isCount, isRecord } from "./contracts.ts";

export type StrategyPlan = {
  signal: string;
  symbol: string;
  exchange: string;
  product: string;
  order_type: string;
  entry_price: string;
  stop_loss: string;
  target_price: string;
  reward_risk: string;
  suggested_quantity: number;
  confidence: number;
  timeframe: string;
  invalidation_reason: string;
  data_freshness: string;
  as_of: string;
};

export type StrategyAgent = {
  node: string;
  role: string;
  status: "ok" | "degraded" | "failed";
  summary: string;
};

export type StrategySymbol = {
  requested: string;
  symbol: string;
  ok: boolean;
  error: string | null;
  signal: string | null;
  composite: number | null;
  mode: string | null;
  data_freshness: string | null;
  plan: StrategyPlan | null;
  agents: StrategyAgent[];
  facts: string[];
  vetoes: string[];
  notes: string[];
};

export type RankedAction = {
  symbol: string;
  action: "BUY" | "HOLD" | "AVOID";
  rank: number;
  size_scale: number;
  rationale: string;
};

export type StrategyDecision = {
  strategy_summary: string;
  ranked_actions: RankedAction[];
  portfolio_notes: string;
  risk_notes: string;
  source: string;
  model: string | null;
  fallback_used: boolean;
  fallback_from: string | null;
};

export type AcceptedPlan = {
  symbol: string;
  rank: number;
  action: string;
  size_scale: number;
  rationale: string;
  notional_paise: number;
  plan: StrategyPlan;
};

export type RejectedPlan = {
  symbol: string;
  action: string;
  reason: string;
};

export type StrategyRun = {
  strategy: string;
  timeframe: string;
  as_of: string;
  spec: {
    strategy: string;
    timeframe: string;
    symbols: string[];
    max_positions: number;
    equity_paise: number;
    available_cash_paise: number;
    risk_basis_points: number;
    playbook: string | null;
    execution: string;
  };
  symbols: StrategySymbol[];
  decision: StrategyDecision;
  accepted: AcceptedPlan[];
  rejected: RejectedPlan[];
  notes: string[];
};

function parsePlan(value: unknown): StrategyPlan {
  if (
    !isRecord(value) ||
    typeof value.signal !== "string" ||
    typeof value.symbol !== "string" ||
    typeof value.exchange !== "string" ||
    typeof value.product !== "string" ||
    value.order_type !== "LIMIT" ||
    typeof value.entry_price !== "string" ||
    typeof value.stop_loss !== "string" ||
    typeof value.target_price !== "string" ||
    typeof value.reward_risk !== "string" ||
    !isCount(value.suggested_quantity) ||
    value.suggested_quantity < 1 ||
    typeof value.confidence !== "number" ||
    typeof value.timeframe !== "string" ||
    typeof value.invalidation_reason !== "string" ||
    typeof value.data_freshness !== "string" ||
    typeof value.as_of !== "string"
  ) {
    throw new Error("Invalid strategy plan");
  }
  return {
    signal: value.signal,
    symbol: value.symbol,
    exchange: value.exchange,
    product: value.product,
    order_type: "LIMIT",
    entry_price: value.entry_price,
    stop_loss: value.stop_loss,
    target_price: value.target_price,
    reward_risk: value.reward_risk,
    suggested_quantity: value.suggested_quantity,
    confidence: value.confidence,
    timeframe: value.timeframe,
    invalidation_reason: value.invalidation_reason,
    data_freshness: value.data_freshness,
    as_of: value.as_of,
  };
}

function parseAgents(value: unknown): StrategyAgent[] {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value) || value.length > 12) {
    throw new Error("Invalid strategy agents");
  }
  return value.map((item) => {
    if (
      !isRecord(item) ||
      typeof item.node !== "string" ||
      typeof item.role !== "string" ||
      (item.status !== "ok" && item.status !== "degraded" && item.status !== "failed") ||
      typeof item.summary !== "string"
    ) {
      throw new Error("Invalid strategy agent");
    }
    return {
      node: item.node,
      role: item.role,
      status: item.status,
      summary: item.summary,
    };
  });
}

function parseSymbol(value: unknown): StrategySymbol {
  if (
    !isRecord(value) ||
    typeof value.requested !== "string" ||
    typeof value.symbol !== "string" ||
    typeof value.ok !== "boolean"
  ) {
    throw new Error("Invalid strategy symbol");
  }
  if (!value.ok) {
    if (typeof value.error !== "string" || value.error.length === 0) {
      throw new Error("Invalid strategy symbol");
    }
    return {
      requested: value.requested,
      symbol: value.symbol,
      ok: false,
      error: value.error,
      signal: null,
      composite: null,
      mode: null,
      data_freshness: null,
      plan: null,
      agents: [],
      facts: [],
      vetoes: [],
      notes: [],
    };
  }
  if (
    typeof value.signal !== "string" ||
    (value.composite !== null &&
      (typeof value.composite !== "number" || !Number.isFinite(value.composite))) ||
    typeof value.mode !== "string" ||
    typeof value.data_freshness !== "string" ||
    !Array.isArray(value.facts) ||
    !value.facts.every((item) => typeof item === "string") ||
    !Array.isArray(value.vetoes) ||
    !value.vetoes.every((item) => typeof item === "string") ||
    !Array.isArray(value.notes) ||
    !value.notes.every((item) => typeof item === "string")
  ) {
    throw new Error("Invalid strategy symbol");
  }
  return {
    requested: value.requested,
    symbol: value.symbol,
    ok: true,
    error: null,
    signal: value.signal,
    composite: value.composite as number | null,
    mode: value.mode,
    data_freshness: value.data_freshness,
    plan: value.plan === null ? null : parsePlan(value.plan),
    agents: parseAgents(value.agents),
    facts: value.facts as string[],
    vetoes: value.vetoes as string[],
    notes: value.notes as string[],
  };
}

function parseRankedAction(value: unknown): RankedAction {
  if (
    !isRecord(value) ||
    typeof value.symbol !== "string" ||
    (value.action !== "BUY" && value.action !== "HOLD" && value.action !== "AVOID") ||
    !isCount(value.rank) ||
    value.rank < 1 ||
    typeof value.size_scale !== "number" ||
    value.size_scale < 0 ||
    value.size_scale > 1 ||
    typeof value.rationale !== "string"
  ) {
    throw new Error("Invalid ranked action");
  }
  return {
    symbol: value.symbol,
    action: value.action,
    rank: value.rank,
    size_scale: value.size_scale,
    rationale: value.rationale,
  };
}

function parseDecision(value: unknown): StrategyDecision {
  if (
    !isRecord(value) ||
    typeof value.strategy_summary !== "string" ||
    !Array.isArray(value.ranked_actions) ||
    value.ranked_actions.length > 8 ||
    typeof value.portfolio_notes !== "string" ||
    typeof value.risk_notes !== "string" ||
    typeof value.source !== "string" ||
    (value.model !== null && typeof value.model !== "string") ||
    typeof value.fallback_used !== "boolean" ||
    (value.fallback_from !== null && typeof value.fallback_from !== "string")
  ) {
    throw new Error("Invalid strategy decision");
  }
  return {
    strategy_summary: value.strategy_summary,
    ranked_actions: value.ranked_actions.map(parseRankedAction),
    portfolio_notes: value.portfolio_notes,
    risk_notes: value.risk_notes,
    source: value.source,
    model: value.model as string | null,
    fallback_used: value.fallback_used,
    fallback_from: value.fallback_from as string | null,
  };
}

function parseAccepted(value: unknown): AcceptedPlan {
  if (
    !isRecord(value) ||
    typeof value.symbol !== "string" ||
    !isCount(value.rank) ||
    typeof value.action !== "string" ||
    typeof value.size_scale !== "number" ||
    typeof value.rationale !== "string" ||
    !isCount(value.notional_paise)
  ) {
    throw new Error("Invalid accepted plan");
  }
  return {
    symbol: value.symbol,
    rank: value.rank,
    action: value.action,
    size_scale: value.size_scale,
    rationale: value.rationale,
    notional_paise: value.notional_paise,
    plan: parsePlan(value.plan),
  };
}

function parseRejected(value: unknown): RejectedPlan {
  if (
    !isRecord(value) ||
    typeof value.symbol !== "string" ||
    typeof value.action !== "string" ||
    typeof value.reason !== "string"
  ) {
    throw new Error("Invalid rejected plan");
  }
  return {
    symbol: value.symbol,
    action: value.action,
    reason: value.reason,
  };
}

export function parseStrategyRun(value: unknown): StrategyRun {
  if (
    !isRecord(value) ||
    typeof value.strategy !== "string" ||
    typeof value.timeframe !== "string" ||
    typeof value.as_of !== "string" ||
    !isRecord(value.spec) ||
    !Array.isArray(value.symbols) ||
    value.symbols.length === 0 ||
    value.symbols.length > 8 ||
    !Array.isArray(value.accepted) ||
    value.accepted.length > 10 ||
    !Array.isArray(value.rejected) ||
    value.rejected.length > 8 ||
    !Array.isArray(value.notes) ||
    !value.notes.every((item) => typeof item === "string")
  ) {
    throw new Error("Invalid strategy run");
  }
  const spec = value.spec;
  if (
    typeof spec.strategy !== "string" ||
    typeof spec.timeframe !== "string" ||
    !Array.isArray(spec.symbols) ||
    !spec.symbols.every((item) => typeof item === "string") ||
    !isCount(spec.max_positions) ||
    !isCount(spec.equity_paise) ||
    !isCount(spec.available_cash_paise) ||
    !isCount(spec.risk_basis_points) ||
    typeof spec.execution !== "string" ||
    (spec.playbook !== undefined &&
      spec.playbook !== null &&
      typeof spec.playbook !== "string")
  ) {
    throw new Error("Invalid strategy spec");
  }
  return {
    strategy: value.strategy,
    timeframe: value.timeframe,
    as_of: value.as_of,
    spec: {
      strategy: spec.strategy,
      timeframe: spec.timeframe,
      symbols: spec.symbols as string[],
      max_positions: spec.max_positions,
      equity_paise: spec.equity_paise,
      available_cash_paise: spec.available_cash_paise,
      risk_basis_points: spec.risk_basis_points,
      playbook:
        spec.playbook === undefined || spec.playbook === null
          ? null
          : (spec.playbook as string),
      execution: spec.execution,
    },
    symbols: value.symbols.map(parseSymbol),
    decision: parseDecision(value.decision),
    accepted: value.accepted.map(parseAccepted),
    rejected: value.rejected.map(parseRejected),
    notes: value.notes as string[],
  };
}
