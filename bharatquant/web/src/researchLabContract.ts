import { isRecord } from "./contracts.ts";

/**
 * Hand-mirrored types for `/api/v1/research/*`. There is no codegen in this
 * app, so each parser validates the shape and throws on drift — the same
 * contract the Node tests import directly.
 */

export const RUN_STATUSES = [
  "queued",
  "running",
  "completed",
  "cancelled",
  "failed",
  "blocked",
] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

export const RESEARCH_AGENT_IDS = [
  "research_lab_planner",
  "research_lab_synthesizer",
  "research_lab_evidence_auditor",
  "research_lab_triage_scorer",
] as const;

export type ResearchSession = {
  session_id: string;
  title: string;
  model_overrides: Record<string, unknown>;
  created_at_utc: string;
  updated_at_utc: string;
};

export type ResearchRun = {
  run_id: string;
  session_id: string;
  parent_run_id: string | null;
  status: RunStatus;
  prompt: string;
  params: Record<string, unknown>;
  metrics: Record<string, unknown>;
  validation: Record<string, unknown>;
  data_manifest_sha256: string | null;
  frozen_data_path: string | null;
  created_at_utc: string;
  completed_at_utc: string | null;
};

export type ResearchEvent = {
  seq: number;
  type: string;
  at: string;
  done: boolean;
  detail: string | null;
  tool: string | null;
  passed: boolean | null;
  reduced: boolean;
  run_id: string | null;
  metrics: string[];
};

export type ResearchRoute = {
  tier: string;
  provider: string | null;
  model: string | null;
};

export type ResearchLabSettings = {
  research_lab: Record<string, unknown>;
  routes: Record<string, ResearchRoute>;
};

export type ResearchArtifact = {
  artifact_id: string;
  slug: string;
  revision: number;
  parent_revision: number | null;
  title: string;
  body_md: string;
  created_at_utc: string;
};

export type ReconcileDiscrepancy = {
  field: string;
  computed: number;
  reported: number;
  difference: number;
};

export type ReconcileResult = {
  passed: boolean;
  discrepancies: ReconcileDiscrepancy[];
  computed_summary: Record<string, unknown>;
};

export type AlphaHorizon = {
  horizon_bars: number;
  ic_mean: number | null;
  rank_ic_mean: number | null;
  icir: number | null;
  turnover: number | null;
  dates: number;
};

export type AlphaBenchResult = {
  horizons: AlphaHorizon[];
  meta: {
    survivorship_bias_warning: boolean;
    membership: string;
    universe_size: number;
    source: string;
    missing_symbols: string[];
  };
};

function str(value: unknown, field: string): string {
  if (typeof value !== "string") {
    throw new Error(`Expected a string for ${field}`);
  }
  return value;
}

function optionalStr(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function record(value: unknown, field: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new Error(`Expected an object for ${field}`);
  }
  return value;
}

export function parseResearchSession(value: unknown): ResearchSession {
  const item = record(value, "session");
  return {
    session_id: str(item.session_id, "session_id"),
    title: str(item.title, "title"),
    model_overrides: isRecord(item.model_overrides) ? item.model_overrides : {},
    created_at_utc: str(item.created_at_utc, "created_at_utc"),
    updated_at_utc: str(item.updated_at_utc, "updated_at_utc"),
  };
}

export function parseResearchRun(value: unknown): ResearchRun {
  const item = record(value, "run");
  const status = str(item.status, "status");
  if (!(RUN_STATUSES as readonly string[]).includes(status)) {
    throw new Error(`Unknown run status: ${status}`);
  }
  return {
    run_id: str(item.run_id, "run_id"),
    session_id: str(item.session_id, "session_id"),
    parent_run_id: optionalStr(item.parent_run_id),
    status: status as RunStatus,
    prompt: str(item.prompt, "prompt"),
    params: isRecord(item.params) ? item.params : {},
    metrics: isRecord(item.metrics) ? item.metrics : {},
    validation: isRecord(item.validation) ? item.validation : {},
    data_manifest_sha256: optionalStr(item.data_manifest_sha256),
    frozen_data_path: optionalStr(item.frozen_data_path),
    created_at_utc: str(item.created_at_utc, "created_at_utc"),
    completed_at_utc: optionalStr(item.completed_at_utc),
  };
}

export function parseResearchEvent(value: unknown): ResearchEvent {
  const item = record(value, "event");
  return {
    seq: typeof item.seq === "number" ? item.seq : 0,
    type: str(item.type, "type"),
    at: typeof item.at === "string" ? item.at : "",
    done: item.done === true,
    detail: optionalStr(item.detail),
    tool: optionalStr(item.tool),
    passed: typeof item.passed === "boolean" ? item.passed : null,
    reduced: item.reduced === true,
    run_id: optionalStr(item.run_id),
    metrics: Array.isArray(item.metrics)
      ? item.metrics.filter((entry): entry is string => typeof entry === "string")
      : [],
  };
}

export function parseResearchLabSettings(value: unknown): ResearchLabSettings {
  const item = record(value, "settings");
  const routes = record(item.routes, "routes");
  const parsedRoutes: Record<string, ResearchRoute> = {};
  for (const agentId of RESEARCH_AGENT_IDS) {
    const route = routes[agentId];
    if (isRecord(route)) {
      parsedRoutes[agentId] = {
        tier: str(route.tier, `${agentId}.tier`),
        provider: optionalStr(route.provider),
        model: optionalStr(route.model),
      };
    }
  }
  return { research_lab: record(item.research_lab, "research_lab"), routes: parsedRoutes };
}

export function parseResearchArtifact(value: unknown): ResearchArtifact {
  const item = record(value, "artifact");
  return {
    artifact_id: str(item.artifact_id, "artifact_id"),
    slug: str(item.slug, "slug"),
    revision: typeof item.revision === "number" ? item.revision : 0,
    parent_revision:
      typeof item.parent_revision === "number" ? item.parent_revision : null,
    title: str(item.title, "title"),
    body_md: str(item.body_md, "body_md"),
    created_at_utc: str(item.created_at_utc, "created_at_utc"),
  };
}

export function parseArtifactList(value: unknown): {
  artifacts: ResearchArtifact[];
  fts_available: boolean;
} {
  const item = record(value, "artifacts");
  const list = Array.isArray(item.artifacts) ? item.artifacts : [];
  return {
    artifacts: list.map(parseResearchArtifact),
    fts_available: item.fts_available === true,
  };
}

export function parseReconcileResult(value: unknown): ReconcileResult {
  const item = record(value, "reconcile");
  const raw = Array.isArray(item.discrepancies) ? item.discrepancies : [];
  return {
    passed: item.passed === true,
    discrepancies: raw.map((entry) => {
      const row = record(entry, "discrepancy");
      return {
        field: str(row.field, "field"),
        computed: typeof row.computed === "number" ? row.computed : 0,
        reported: typeof row.reported === "number" ? row.reported : 0,
        difference: typeof row.difference === "number" ? row.difference : 0,
      };
    }),
    computed_summary: isRecord(item.computed_summary) ? item.computed_summary : {},
  };
}

export function parseAlphaBenchResult(value: unknown): AlphaBenchResult {
  const item = record(value, "bench");
  const meta = record(item._meta, "_meta");
  const horizons = Array.isArray(item.horizons) ? item.horizons : [];
  return {
    horizons: horizons.map((entry) => {
      const row = record(entry, "horizon");
      return {
        horizon_bars: typeof row.horizon_bars === "number" ? row.horizon_bars : 0,
        ic_mean: numOrNull(row.ic_mean),
        rank_ic_mean: numOrNull(row.rank_ic_mean),
        icir: numOrNull(row.icir),
        turnover: numOrNull(row.turnover),
        dates: typeof row.dates === "number" ? row.dates : 0,
      };
    }),
    meta: {
      survivorship_bias_warning: meta.survivorship_bias_warning === true,
      membership: typeof meta.membership === "string" ? meta.membership : "unknown",
      universe_size:
        typeof meta.universe_size === "number" ? meta.universe_size : 0,
      source: typeof meta.source === "string" ? meta.source : "unknown",
      missing_symbols: Array.isArray(meta.missing_symbols)
        ? meta.missing_symbols.filter(
            (entry): entry is string => typeof entry === "string",
          )
        : [],
    },
  };
}
