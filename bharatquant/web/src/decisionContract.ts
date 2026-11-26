import { isCount, isRecord } from "./contracts.ts";

export type DecisionJudgment = {
  name: string;
  kind: "choice" | "score" | "noul";
  choice: string | null;
  score: number | null;
  noul: number | null;
  confidence: number | null;
};

export type DecisionScreener = {
  verdict: "research" | "skip";
  reason: string;
  quality: number;
  worth: number;
  confidence: number;
};

export type DecisionRoute = {
  tier: string;
  provider: string;
  model: string;
  decision_source: string;
  fallback_used: boolean;
  fallback_from: string | null;
  latency_ms: number | null;
  request_id: string | null;
  model_mismatch: boolean;
  state_hash: string | null;
};

export type DecisionProvenance = {
  vendor_daily: string;
  vendor_quotes: string;
  is_realtime: boolean;
  data_freshness: string;
};

export type DecisionPreview = {
  symbol: string;
  timeframe: string;
  as_of: string;
  decision_preview: string;
  data_provenance: DecisionProvenance;
  state: Record<string, string | number>;
  judgments: DecisionJudgment[];
  screener: DecisionScreener;
  route: DecisionRoute;
  notes: string[];
};

function isNumberOrNull(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isFinite(value));
}

function parseJudgment(value: unknown): DecisionJudgment {
  if (
    !isRecord(value) ||
    typeof value.name !== "string" ||
    value.name.length === 0 ||
    (value.kind !== "choice" && value.kind !== "score" && value.kind !== "noul") ||
    (value.choice !== null && typeof value.choice !== "string") ||
    (value.score !== null && !isCount(value.score)) ||
    !isNumberOrNull(value.noul) ||
    (value.noul !== null && (value.noul < 0 || value.noul > 1)) ||
    !isNumberOrNull(value.confidence) ||
    (value.confidence !== null &&
      (value.confidence < 0 || value.confidence > 1))
  ) {
    throw new Error("Invalid decision judgment");
  }
  return {
    name: value.name,
    kind: value.kind,
    choice: value.choice as string | null,
    score: value.score as number | null,
    noul: value.noul,
    confidence: value.confidence,
  };
}

function parseScreener(value: unknown): DecisionScreener {
  if (
    !isRecord(value) ||
    (value.verdict !== "research" && value.verdict !== "skip") ||
    typeof value.reason !== "string" ||
    typeof value.quality !== "number" ||
    typeof value.worth !== "number" ||
    typeof value.confidence !== "number"
  ) {
    throw new Error("Invalid screener decision");
  }
  return {
    verdict: value.verdict,
    reason: value.reason,
    quality: value.quality,
    worth: value.worth,
    confidence: value.confidence,
  };
}

function parseRoute(value: unknown): DecisionRoute {
  if (
    !isRecord(value) ||
    typeof value.tier !== "string" ||
    typeof value.provider !== "string" ||
    typeof value.model !== "string" ||
    typeof value.decision_source !== "string" ||
    typeof value.fallback_used !== "boolean" ||
    (value.fallback_from !== null && typeof value.fallback_from !== "string") ||
    !isNumberOrNull(value.latency_ms) ||
    (value.latency_ms !== null && value.latency_ms < 0) ||
    (value.request_id !== null && typeof value.request_id !== "string") ||
    typeof value.model_mismatch !== "boolean" ||
    (value.state_hash !== null && typeof value.state_hash !== "string")
  ) {
    throw new Error("Invalid decision route");
  }
  return {
    tier: value.tier,
    provider: value.provider,
    model: value.model,
    decision_source: value.decision_source,
    fallback_used: value.fallback_used,
    fallback_from: value.fallback_from as string | null,
    latency_ms: value.latency_ms,
    request_id: value.request_id as string | null,
    model_mismatch: value.model_mismatch,
    state_hash: value.state_hash as string | null,
  };
}

function parseProvenance(value: unknown): DecisionProvenance {
  if (
    !isRecord(value) ||
    typeof value.vendor_daily !== "string" ||
    typeof value.vendor_quotes !== "string" ||
    typeof value.is_realtime !== "boolean" ||
    typeof value.data_freshness !== "string"
  ) {
    throw new Error("Invalid decision provenance");
  }
  return {
    vendor_daily: value.vendor_daily,
    vendor_quotes: value.vendor_quotes,
    is_realtime: value.is_realtime,
    data_freshness: value.data_freshness,
  };
}

function parseState(value: unknown): Record<string, string | number> {
  if (!isRecord(value)) {
    throw new Error("Invalid decision state");
  }
  const state: Record<string, string | number> = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item === "number") {
      if (!Number.isFinite(item)) {
        throw new Error("Invalid decision state");
      }
      state[key] = item;
    } else if (typeof item === "string") {
      state[key] = item;
    } else {
      throw new Error("Invalid decision state");
    }
  }
  return state;
}

export function parseDecisionPreview(value: unknown): DecisionPreview {
  if (
    !isRecord(value) ||
    typeof value.symbol !== "string" ||
    typeof value.timeframe !== "string" ||
    typeof value.as_of !== "string" ||
    typeof value.decision_preview !== "string" ||
    !isRecord(value.data_provenance) ||
    !isRecord(value.state) ||
    !Array.isArray(value.judgments) ||
    value.judgments.length === 0 ||
    value.judgments.length > 8 ||
    !Array.isArray(value.notes) ||
    !value.notes.every((item) => typeof item === "string")
  ) {
    throw new Error("Invalid decision preview");
  }
  return {
    symbol: value.symbol,
    timeframe: value.timeframe,
    as_of: value.as_of,
    decision_preview: value.decision_preview,
    data_provenance: parseProvenance(value.data_provenance),
    state: parseState(value.state),
    judgments: value.judgments.map(parseJudgment),
    screener: parseScreener(value.screener),
    route: parseRoute(value.route),
    notes: value.notes as string[],
  };
}
