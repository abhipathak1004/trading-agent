import { isCount, isRecord } from "./contracts.ts";
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

export type SpecialistClaim = {
  statement: string;
  evidence_ids: string[];
  verified: boolean;
};

export type SpecialistBrief = {
  agent: string;
  symbol: string;
  as_of: string;
  facts: string[];
  claims: SpecialistClaim[];
  degraded: boolean;
  source: string;
};

export type ConfidenceEvidence = {
  family: string;
  state: string;
  strength: number;
};

export type ConfidenceBaseRate = {
  setup_id: string;
  symbol: string;
  regime: string;
  horizon_days: number;
  sample_count: number;
  win_count: number;
  win_rate: number;
  data_as_of: string;
};

export type Confidence = {
  probability: number;
  level: string;
  disposition: string;
  confirming_families: number;
  base_rate_samples: number;
  live_outcomes: number;
  calibrated: boolean;
  ev_r_after_costs: number;
  vetoes: string[];
  evidence: ConfidenceEvidence[];
  base_rate: ConfidenceBaseRate;
};

export type EvidenceVerification = {
  verified_claims: number;
  unverified_claims: number;
  missing_evidence_ids: string[];
  decision_source: string;
  model: string | null;
};

export type FinalGate = {
  action: string;
  execute: boolean;
  size_scale: number;
  reason: string;
  conviction: number;
  source: string;
  provider: string;
  model: string;
  tier: string;
  fallback_used: boolean;
  fallback_from: string | null;
  latency_ms: number | null;
  request_id: string | null;
  model_mismatch: boolean;
  state_hash: string | null;
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
  specialists: SpecialistBrief[];
  evidence: EvidenceVerification | null;
  final_gate: FinalGate | null;
  confidence: Confidence | null;
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

function parseSpecialistClaims(value: unknown): SpecialistClaim[] {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value) || value.length > 8) {
    throw new Error("Invalid specialist claims");
  }
  return value.map((claim) => {
    if (
      !isRecord(claim) ||
      typeof claim.statement !== "string" ||
      !Array.isArray(claim.evidence_ids) ||
      !claim.evidence_ids.every((item) => typeof item === "string") ||
      typeof claim.verified !== "boolean"
    ) {
      throw new Error("Invalid specialist claim");
    }
    return {
      statement: claim.statement,
      evidence_ids: claim.evidence_ids as string[],
      verified: claim.verified,
    };
  });
}

function parseSpecialistBriefs(value: unknown): SpecialistBrief[] {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value) || value.length > 4) {
    throw new Error("Invalid specialist briefs");
  }
  return value.map((brief) => {
    if (
      !isRecord(brief) ||
      typeof brief.agent !== "string" ||
      typeof brief.symbol !== "string" ||
      typeof brief.as_of !== "string" ||
      !Array.isArray(brief.facts) ||
      brief.facts.length > 10 ||
      !brief.facts.every((item) => typeof item === "string") ||
      typeof brief.degraded !== "boolean" ||
      typeof brief.source !== "string"
    ) {
      throw new Error("Invalid specialist brief");
    }
    return {
      agent: brief.agent,
      symbol: brief.symbol,
      as_of: brief.as_of,
      facts: brief.facts as string[],
      claims: parseSpecialistClaims(brief.claims),
      degraded: brief.degraded,
      source: brief.source,
    };
  });
}

export function parseConfidence(value: unknown): Confidence | null {
  if (value === undefined || value === null) {
    return null;
  }
  if (
    !isRecord(value) ||
    typeof value.probability !== "number" ||
    !Number.isFinite(value.probability) ||
    typeof value.level !== "string" ||
    typeof value.disposition !== "string" ||
    !isCount(value.confirming_families) ||
    !isCount(value.base_rate_samples) ||
    !isCount(value.live_outcomes) ||
    typeof value.calibrated !== "boolean" ||
    typeof value.ev_r_after_costs !== "number" ||
    !Number.isFinite(value.ev_r_after_costs) ||
    !Array.isArray(value.vetoes) ||
    !value.vetoes.every((item) => typeof item === "string") ||
    !Array.isArray(value.evidence) ||
    value.evidence.length > 7 ||
    !isRecord(value.base_rate)
  ) {
    throw new Error("Invalid confidence result");
  }
  const baseRate = value.base_rate;
  if (
    typeof baseRate.setup_id !== "string" ||
    typeof baseRate.symbol !== "string" ||
    typeof baseRate.regime !== "string" ||
    !isCount(baseRate.horizon_days) ||
    !isCount(baseRate.sample_count) ||
    !isCount(baseRate.win_count) ||
    typeof baseRate.win_rate !== "number" ||
    !Number.isFinite(baseRate.win_rate) ||
    typeof baseRate.data_as_of !== "string"
  ) {
    throw new Error("Invalid confidence base rate");
  }
  return {
    probability: value.probability,
    level: value.level,
    disposition: value.disposition,
    confirming_families: value.confirming_families,
    base_rate_samples: value.base_rate_samples,
    live_outcomes: value.live_outcomes,
    calibrated: value.calibrated,
    ev_r_after_costs: value.ev_r_after_costs,
    vetoes: value.vetoes as string[],
    evidence: value.evidence.map((item) => {
      if (
        !isRecord(item) ||
        typeof item.family !== "string" ||
        typeof item.state !== "string" ||
        typeof item.strength !== "number" ||
        !Number.isFinite(item.strength)
      ) {
        throw new Error("Invalid confidence evidence");
      }
      return {
        family: item.family,
        state: item.state,
        strength: item.strength,
      };
    }),
    base_rate: {
      setup_id: baseRate.setup_id,
      symbol: baseRate.symbol,
      regime: baseRate.regime,
      horizon_days: baseRate.horizon_days,
      sample_count: baseRate.sample_count,
      win_count: baseRate.win_count,
      win_rate: baseRate.win_rate,
      data_as_of: baseRate.data_as_of,
    },
  };
}

function parseEvidence(value: unknown): EvidenceVerification | null {
  if (value === undefined || value === null) {
    return null;
  }
  if (
    !isRecord(value) ||
    !isCount(value.verified_claims) ||
    !isCount(value.unverified_claims) ||
    !Array.isArray(value.missing_evidence_ids) ||
    !value.missing_evidence_ids.every((item) => typeof item === "string")
  ) {
    throw new Error("Invalid evidence verification");
  }
  return {
    verified_claims: value.verified_claims,
    unverified_claims: value.unverified_claims,
    missing_evidence_ids: value.missing_evidence_ids as string[],
    decision_source:
      typeof value.decision_source === "string"
        ? value.decision_source
        : "citation",
    model: typeof value.model === "string" ? value.model : null,
  };
}

function parseFinalGate(value: unknown): FinalGate | null {
  if (value === undefined || value === null) {
    return null;
  }
  if (!isRecord(value)) {
    throw new Error("Invalid final gate");
  }
  const conviction = value.conviction;
  if (
    typeof value.action !== "string" ||
    value.action.length === 0 ||
    typeof value.execute !== "boolean" ||
    typeof value.size_scale !== "number" ||
    typeof value.reason !== "string" ||
    !isCount(conviction) ||
    typeof value.source !== "string" ||
    typeof value.provider !== "string" ||
    typeof value.model !== "string" ||
    typeof value.tier !== "string" ||
    typeof value.fallback_used !== "boolean" ||
    (value.fallback_from !== null && typeof value.fallback_from !== "string") ||
    (value.latency_ms !== null &&
      (typeof value.latency_ms !== "number" || value.latency_ms < 0)) ||
    (value.request_id !== null && typeof value.request_id !== "string") ||
    typeof value.model_mismatch !== "boolean" ||
    (value.state_hash !== null && typeof value.state_hash !== "string")
  ) {
    throw new Error("Invalid final gate");
  }
  return {
    action: value.action,
    execute: value.execute,
    size_scale: value.size_scale,
    reason: value.reason,
    conviction,
    source: value.source,
    provider: value.provider,
    model: value.model,
    tier: value.tier,
    fallback_used: value.fallback_used,
    fallback_from: value.fallback_from as string | null,
    latency_ms: value.latency_ms as number | null,
    request_id: value.request_id as string | null,
    model_mismatch: value.model_mismatch,
    state_hash: value.state_hash as string | null,
  };
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
    specialists: parseSpecialistBriefs(value.specialists),
    evidence: parseEvidence(value.evidence),
    final_gate: parseFinalGate(value.final_gate),
    confidence: parseConfidence(value.confidence),
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
