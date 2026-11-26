import { isCount, isRecord, isTimestamp } from "./contracts.ts";
import { parseConfidence } from "./analysisContract.ts";
import type { Confidence } from "./analysisContract.ts";
import { parsePlan } from "./strategyContract.ts";
import type { StrategyPlan } from "./strategyContract.ts";

export type Opportunity = {
  opportunity_id: string;
  instrument_id: string;
  horizon: string;
  side: string;
  created_at: string;
  probability: number;
  level: string;
  disposition: string;
  confirming_families: number;
  base_rate_samples: number;
  calibrated: boolean;
  ev_r_after_costs: number;
  vetoes: string[];
  assessment_hash: string;
  score: number;
  run_id: string | null;
  source: string | null;
  setup_id: string | null;
  confidence: Confidence | null;
  plan: StrategyPlan | null;
  lifecycle: string;
};

export type OpportunityList = {
  opportunities: Opportunity[];
};

export type ShadowSummary = {
  verdict: string;
  suggested_count: number;
  unsuggested_count: number;
  suggested_win_rate: number | null;
  unsuggested_win_rate: number | null;
  suggested_interval: number[] | null;
  unsuggested_interval: number[] | null;
};

export function parseOpportunity(value: unknown): Opportunity {
  if (
    !isRecord(value) ||
    typeof value.opportunity_id !== "string" ||
    typeof value.instrument_id !== "string" ||
    typeof value.horizon !== "string" ||
    typeof value.side !== "string" ||
    !isTimestamp(value.created_at) ||
    typeof value.probability !== "number" ||
    !Number.isFinite(value.probability) ||
    typeof value.level !== "string" ||
    typeof value.disposition !== "string" ||
    !isCount(value.confirming_families) ||
    !isCount(value.base_rate_samples) ||
    typeof value.calibrated !== "boolean" ||
    typeof value.ev_r_after_costs !== "number" ||
    !Number.isFinite(value.ev_r_after_costs) ||
    !Array.isArray(value.vetoes) ||
    !value.vetoes.every((item) => typeof item === "string") ||
    typeof value.assessment_hash !== "string" ||
    typeof value.score !== "number" ||
    !Number.isFinite(value.score) ||
    (typeof value.run_id !== "string" && value.run_id !== null) ||
    (typeof value.source !== "string" && value.source !== null) ||
    (typeof value.setup_id !== "string" && value.setup_id !== null) ||
    (typeof value.lifecycle !== "string" && value.lifecycle !== null)
  ) {
    throw new Error("Invalid opportunity");
  }
  return {
    opportunity_id: value.opportunity_id,
    instrument_id: value.instrument_id,
    horizon: value.horizon,
    side: value.side,
    created_at: value.created_at,
    probability: value.probability,
    level: value.level,
    disposition: value.disposition,
    confirming_families: value.confirming_families,
    base_rate_samples: value.base_rate_samples,
    calibrated: value.calibrated,
    ev_r_after_costs: value.ev_r_after_costs,
    vetoes: value.vetoes as string[],
    assessment_hash: value.assessment_hash,
    score: value.score,
    run_id: value.run_id as string | null,
    source: value.source as string | null,
    setup_id: value.setup_id as string | null,
    confidence: parseConfidence(value.confidence),
    plan: value.plan === null ? null : parsePlan(value.plan),
    lifecycle: value.lifecycle as string,
  };
}

export function parseOpportunityList(value: unknown): OpportunityList {
  if (!isRecord(value) || !Array.isArray(value.opportunities)) {
    throw new Error("Invalid opportunity list");
  }
  return {
    opportunities: value.opportunities.map(parseOpportunity),
  };
}

export function parseShadowSummary(value: unknown): ShadowSummary {
  if (
    !isRecord(value) ||
    typeof value.verdict !== "string" ||
    !isCount(value.suggested_count) ||
    !isCount(value.unsuggested_count) ||
    (value.suggested_win_rate !== null &&
      typeof value.suggested_win_rate !== "number") ||
    (value.unsuggested_win_rate !== null &&
      typeof value.unsuggested_win_rate !== "number") ||
    (value.suggested_interval !== null &&
      (!Array.isArray(value.suggested_interval) ||
        value.suggested_interval.length !== 2)) ||
    (value.unsuggested_interval !== null &&
      (!Array.isArray(value.unsuggested_interval) ||
        value.unsuggested_interval.length !== 2))
  ) {
    throw new Error("Invalid shadow summary");
  }
  return {
    verdict: value.verdict,
    suggested_count: value.suggested_count,
    unsuggested_count: value.unsuggested_count,
    suggested_win_rate: value.suggested_win_rate as number | null,
    unsuggested_win_rate: value.unsuggested_win_rate as number | null,
    suggested_interval: value.suggested_interval as number[] | null,
    unsuggested_interval: value.unsuggested_interval as number[] | null,
  };
}
