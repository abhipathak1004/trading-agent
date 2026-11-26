import { isRecord } from "./contracts.ts";
export type AnalysisSubScores = {
  trend: number | null;
  momentum: number | null;
  volatility: number | null;
  structure: number | null;
};

export type AnalysisProvenance = {
  vendor_daily: string;
  vendor_quotes: string;
  is_realtime: boolean;
  data_freshness: string;
};

export type TradePlan = {
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

export type AgentTraceEntry = {
  node: string;
  role: string;
  status: "ok" | "degraded" | "failed";
  summary: string;
};

export type AnalysisRun = {
  /** Absent when the payload has not been persisted to the run store yet. */
  run_id?: string;
  symbol: string;
  timeframe: string;
  mode: string;
  as_of: string;
  data_provenance: AnalysisProvenance;
  facts: string[];
  composite: number | null;
  sub_scores: AnalysisSubScores;
  plan: TradePlan | null;
  vetoes: string[];
  notes: string[];
  agents: AgentTraceEntry[];
};

function isNumberOrNull(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isFinite(value));
}

function parseStringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw new Error(`Invalid ${label}`);
  }
  return value as string[];
}

function parsePlan(value: unknown): TradePlan | null {
  if (value === null) {
    return null;
  }
  if (
    !isRecord(value) ||
    typeof value.signal !== "string" ||
    typeof value.symbol !== "string" ||
    typeof value.exchange !== "string" ||
    typeof value.product !== "string" ||
    typeof value.order_type !== "string" ||
    typeof value.entry_price !== "string" ||
    typeof value.stop_loss !== "string" ||
    typeof value.target_price !== "string" ||
    typeof value.reward_risk !== "string" ||
    typeof value.suggested_quantity !== "number" ||
    !Number.isSafeInteger(value.suggested_quantity) ||
    value.suggested_quantity <= 0 ||
    typeof value.confidence !== "number" ||
    typeof value.timeframe !== "string" ||
    typeof value.invalidation_reason !== "string" ||
    typeof value.data_freshness !== "string" ||
    typeof value.as_of !== "string"
  ) {
    throw new Error("Invalid trade plan");
  }
  // Only LIMIT orders are ever produced; anything else means the contract moved.
  if (value.order_type !== "LIMIT") {
    throw new Error("Invalid trade plan order type");
  }
  return value as TradePlan;
}

function parseAgents(value: unknown): AgentTraceEntry[] {
  if (value === undefined) {
    // Older payloads predate the trace; treat as empty rather than failing.
    return [];
  }
  if (!Array.isArray(value)) {
    throw new Error("Invalid agent trace");
  }
  return value.map((entry) => {
    if (
      !isRecord(entry) ||
      typeof entry.node !== "string" ||
      typeof entry.role !== "string" ||
      typeof entry.status !== "string" ||
      typeof entry.summary !== "string" ||
      !["ok", "degraded", "failed"].includes(entry.status)
    ) {
      throw new Error("Invalid agent trace entry");
    }
    return {
      node: entry.node,
      role: entry.role,
      status: entry.status as AgentTraceEntry["status"],
      summary: entry.summary,
    };
  });
}

export function parseAnalysisRun(value: unknown): AnalysisRun {
  if (
    !isRecord(value) ||
    typeof value.symbol !== "string" ||
    typeof value.timeframe !== "string" ||
    typeof value.mode !== "string" ||
    typeof value.as_of !== "string" ||
    !isRecord(value.data_provenance)
  ) {
    throw new Error("Invalid analysis run");
  }
  // `run_id` is optional because the pipeline payload predates the run store.
  // The API attaches it when it persists the run; a payload without one is
  // still perfectly renderable, so its absence must not reject the whole card.
  if (value.run_id !== undefined && typeof value.run_id !== "string") {
    throw new Error("Invalid analysis run id");
  }
  const provenance = value.data_provenance;
  if (
    typeof provenance.vendor_daily !== "string" ||
    typeof provenance.vendor_quotes !== "string" ||
    typeof provenance.is_realtime !== "boolean" ||
    typeof provenance.data_freshness !== "string"
  ) {
    throw new Error("Invalid analysis provenance");
  }
  if (!isRecord(value.report)) {
    throw new Error("Invalid analysis report");
  }
  const report = value.report;
  if (!isRecord(report.sub_scores)) {
    throw new Error("Invalid analysis sub-scores");
  }
  const subScores = report.sub_scores;
  for (const key of ["trend", "momentum", "volatility", "structure"] as const) {
    if (!isNumberOrNull(subScores[key])) {
      throw new Error("Invalid analysis sub-score value");
    }
  }
  return {
    run_id: typeof value.run_id === "string" ? value.run_id : undefined,
    symbol: value.symbol,
    timeframe: value.timeframe,
    mode: value.mode,
    as_of: value.as_of,
    data_provenance: {
      vendor_daily: provenance.vendor_daily,
      vendor_quotes: provenance.vendor_quotes,
      is_realtime: provenance.is_realtime,
      data_freshness: provenance.data_freshness,
    },
    facts: parseStringArray(report.facts, "analysis facts"),
    composite: isNumberOrNull(report.composite) ? report.composite : null,
    sub_scores: {
      trend: subScores.trend as number | null,
      momentum: subScores.momentum as number | null,
      volatility: subScores.volatility as number | null,
      structure: subScores.structure as number | null,
    },
    plan: parsePlan(value.plan),
    vetoes: parseStringArray(value.vetoes, "analysis vetoes"),
    notes: parseStringArray(value.notes, "analysis notes"),
    agents: parseAgents(value.agents),
  };
}

export type VendorChains = {
  kite_enabled: boolean;
  chains: Array<{ domain: string; vendors: string[] }>;
};

export function parseVendorChains(value: unknown): VendorChains {
  if (
    !isRecord(value) ||
    typeof value.kite_enabled !== "boolean" ||
    !isRecord(value.chains)
  ) {
    throw new Error("Invalid vendor chains");
  }
  const chains = Object.entries(value.chains).map(([domain, vendors]) => ({
    domain,
    vendors: parseStringArray(vendors, "vendor chain"),
  }));
  return { kite_enabled: value.kite_enabled, chains };
}

export type BatchAnalysisItem = {
  requested: string;
  symbol: string;
  ok: boolean;
  error: string | null;
  analysis: AnalysisRun | null;
};

export type BatchAnalysis = {
  timeframe: string;
  as_of: string;
  requested: number;
  succeeded: number;
  failed: number;
  items: BatchAnalysisItem[];
};

export function parseBatchAnalysis(value: unknown): BatchAnalysis {
  if (
    !isRecord(value) ||
    typeof value.timeframe !== "string" ||
    typeof value.as_of !== "string" ||
    typeof value.requested !== "number" ||
    typeof value.succeeded !== "number" ||
    typeof value.failed !== "number" ||
    !Array.isArray(value.items)
  ) {
    throw new Error("Invalid batch analysis");
  }
  const items = value.items.map((item) => {
    if (
      !isRecord(item) ||
      typeof item.requested !== "string" ||
      typeof item.symbol !== "string" ||
      typeof item.ok !== "boolean" ||
      (item.error !== null && typeof item.error !== "string") ||
      (item.analysis !== null && !isRecord(item.analysis))
    ) {
      throw new Error("Invalid batch item");
    }
    if (item.ok && item.analysis === null) {
      throw new Error("A successful batch item must carry an analysis");
    }
    if (!item.ok && item.error === null) {
      throw new Error("A failed batch item must carry an error");
    }
    return {
      requested: item.requested,
      symbol: item.symbol,
      ok: item.ok,
      error: item.error as string | null,
      analysis: item.analysis === null ? null : parseAnalysisRun(item.analysis),
    };
  });
  return {
    timeframe: value.timeframe,
    as_of: value.as_of,
    requested: value.requested,
    succeeded: value.succeeded,
    failed: value.failed,
    items,
  };
}
