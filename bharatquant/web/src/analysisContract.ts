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

export type AnalysisRun = {
  run_id: string;
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
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

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

export function parseAnalysisRun(value: unknown): AnalysisRun {
  if (
    !isRecord(value) ||
    typeof value.run_id !== "string" ||
    value.run_id.length === 0 ||
    typeof value.symbol !== "string" ||
    typeof value.timeframe !== "string" ||
    typeof value.mode !== "string" ||
    typeof value.as_of !== "string" ||
    !isRecord(value.data_provenance)
  ) {
    throw new Error("Invalid analysis run");
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
    run_id: value.run_id,
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
