import { formatUtc, isRecord } from "./contracts.ts";

export type EvalProvider = { id: string; label: string; model: string };

export type EvalConfig = {
  providers: EvalProvider[];
  categories: string[];
  tools: { npx: boolean; ollama: boolean };
  config_path: string;
  dataset_path: string;
  active_run_id: string | null;
  custom_endpoint: string | null;
};

export type EvalModelScore = {
  model: string;
  total_cases: number;
  overall_pass_rate: number;
  accuracy_pass_rate: number;
  json_parse_rate: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
  max_ms: number | null;
  brier_score: number | null;
  ece_5bin: number | null;
  brier_n: number;
  category_rates: Record<string, number | null>;
};

export type EvalRun = {
  id: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  status: string;
  models: string[];
  categories: string[];
  config_path: string;
  results_json_path: string | null;
  error_message: string | null;
  scores: EvalModelScore[];
};

export type EvalCase = {
  description: string;
  model: string;
  category: string;
  passed: boolean;
  input: string;
  raw_output: string;
  confidence: number | null;
  latency_ms: number | null;
  failure_reasons: string[];
};

export type EvalCaseList = {
  run_id: string;
  model: string | null;
  category: string | null;
  status: string;
  total: number;
  cases: EvalCase[];
};

export type EvalPromoteDiff = {
  agent_id: string;
  before: { tier: string; provider: string | null; model: string | null };
  after: { tier: string; provider: string | null; model: string | null };
  yaml_before: string;
  yaml_after: string;
};

export const RUN_STATUSES = ["queued", "running", "completed", "failed", "cancelled"];

export type ScoreColumn =
  | "overall_pass_rate"
  | "accuracy_pass_rate"
  | "json_parse_rate"
  | "p50_ms"
  | "p95_ms"
  | "brier_score"
  | "ece_5bin";

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function score(value: unknown): EvalModelScore {
  const item = isRecord(value) ? value : {};
  const rates: Record<string, number | null> = {};
  if (isRecord(item.category_rates)) {
    for (const [key, entry] of Object.entries(item.category_rates)) {
      rates[key] = num(entry);
    }
  }
  return {
    model: text(item.model, "unknown"),
    total_cases: num(item.total_cases) ?? 0,
    overall_pass_rate: num(item.overall_pass_rate) ?? 0,
    accuracy_pass_rate: num(item.accuracy_pass_rate) ?? 0,
    json_parse_rate: num(item.json_parse_rate),
    p50_ms: num(item.p50_ms),
    p95_ms: num(item.p95_ms),
    max_ms: num(item.max_ms),
    brier_score: num(item.brier_score),
    ece_5bin: num(item.ece_5bin),
    brier_n: num(item.brier_n) ?? 0,
    category_rates: rates,
  };
}

export function parseEvalConfig(value: unknown): EvalConfig {
  const item = isRecord(value) ? value : {};
  const providers = Array.isArray(item.providers)
    ? item.providers.filter(isRecord).map((entry) => ({
        id: text(entry.id),
        label: text(entry.label, text(entry.id)),
        model: text(entry.model),
      }))
    : [];
  const categories = Array.isArray(item.categories)
    ? item.categories.filter((entry): entry is string => typeof entry === "string")
    : [];
  const tools = isRecord(item.tools) ? item.tools : {};
  return {
    providers,
    categories,
    tools: { npx: tools.npx === true, ollama: tools.ollama === true },
    config_path: text(item.config_path),
    dataset_path: text(item.dataset_path),
    active_run_id: typeof item.active_run_id === "string" ? item.active_run_id : null,
    custom_endpoint:
      typeof item.custom_endpoint === "string" ? item.custom_endpoint : null,
  };
}

function parseRun(value: unknown): EvalRun | null {
  if (!isRecord(value)) {
    return null;
  }
  const status = text(value.status, "unknown");
  return {
    id: text(value.id),
    created_at: text(value.created_at),
    started_at: typeof value.started_at === "string" ? value.started_at : null,
    completed_at: typeof value.completed_at === "string" ? value.completed_at : null,
    status,
    models: Array.isArray(value.models)
      ? value.models.filter((entry): entry is string => typeof entry === "string")
      : [],
    categories: Array.isArray(value.categories)
      ? value.categories.filter((entry): entry is string => typeof entry === "string")
      : [],
    config_path: text(value.config_path),
    results_json_path:
      typeof value.results_json_path === "string" ? value.results_json_path : null,
    error_message:
      typeof value.error_message === "string" ? value.error_message : null,
    scores: Array.isArray(value.scores) ? value.scores.map(score) : [],
  };
}

export function parseEvalRuns(value: unknown): EvalRun[] {
  const item = isRecord(value) ? value : {};
  const runs = Array.isArray(item.runs) ? item.runs : [];
  return runs.map(parseRun).filter((run): run is EvalRun => run !== null);
}

export function parseEvalRun(value: unknown): EvalRun {
  const run = parseRun(value);
  if (run === null) {
    throw new Error("Invalid eval run response");
  }
  return run;
}

export function parseEvalCases(value: unknown): EvalCaseList {
  const item = isRecord(value) ? value : {};
  const cases = Array.isArray(item.cases)
    ? item.cases.filter(isRecord).map((entry) => ({
        description: text(entry.description),
        model: text(entry.model),
        category: text(entry.category),
        passed: entry.passed === true,
        input: text(entry.input),
        raw_output: text(entry.raw_output),
        confidence: num(entry.confidence),
        latency_ms: num(entry.latency_ms),
        failure_reasons: Array.isArray(entry.failure_reasons)
          ? entry.failure_reasons.filter(
              (reason): reason is string => typeof reason === "string",
            )
          : [],
      }))
    : [];
  return {
    run_id: text(item.run_id),
    model: typeof item.model === "string" ? item.model : null,
    category: typeof item.category === "string" ? item.category : null,
    status: text(item.status, "all"),
    total: num(item.total) ?? cases.length,
    cases,
  };
}

export function parsePromoteDiff(value: unknown): EvalPromoteDiff {
  const item = isRecord(value) ? value : {};
  const route = (entry: unknown) => {
    const record = isRecord(entry) ? entry : {};
    return {
      tier: text(record.tier),
      provider: typeof record.provider === "string" ? record.provider : null,
      model: typeof record.model === "string" ? record.model : null,
    };
  };
  return {
    agent_id: text(item.agent_id),
    before: route(item.before),
    after: route(item.after),
    yaml_before: text(item.yaml_before),
    yaml_after: text(item.yaml_after),
  };
}

export function formatPercent(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export function formatMs(value: number | null): string {
  return value === null ? "—" : `${Math.round(value)} ms`;
}

export function formatScore(value: number | null, decimals = 3): string {
  return value === null ? "—" : value.toFixed(decimals);
}

export function rateTone(value: number | null): "ok" | "warn" | "danger" | "muted" {
  if (value === null) {
    return "muted";
  }
  if (value >= 0.85) {
    return "ok";
  }
  if (value >= 0.65) {
    return "warn";
  }
  return "danger";
}

export function runStatusTone(status: string): "ok" | "warn" | "danger" | "muted" {
  if (status === "completed") {
    return "ok";
  }
  if (status === "queued" || status === "running") {
    return "warn";
  }
  if (status === "failed" || status === "cancelled") {
    return "danger";
  }
  return "muted";
}

// Higher is better for rates; lower is better for latency and calibration.
const LOWER_IS_BETTER: ReadonlySet<ScoreColumn> = new Set<ScoreColumn>([
  "p50_ms",
  "p95_ms",
  "brier_score",
  "ece_5bin",
]);

export function bestValue(
  scores: EvalModelScore[],
  column: ScoreColumn,
): number | null {
  const values = scores
    .map((entry) => entry[column])
    .filter((value): value is number => value !== null);
  if (values.length === 0) {
    return null;
  }
  return LOWER_IS_BETTER.has(column)
    ? Math.min(...values)
    : Math.max(...values);
}

export function isBest(
  scores: EvalModelScore[],
  column: ScoreColumn,
  value: number | null,
): boolean {
  if (value === null) {
    return false;
  }
  const best = bestValue(scores, column);
  return best !== null && value === best && scores.length > 1;
}

export function runSummary(run: EvalRun): string {
  const when = run.completed_at ?? run.started_at ?? run.created_at;
  const stamp = when === "" ? "" : formatUtc(when);
  const models = run.models.length === 1 ? "1 model" : `${run.models.length} models`;
  return `${stamp} · ${run.status} · ${models}`;
}
