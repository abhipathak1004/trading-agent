import { isCount, isRecord } from "./contracts.ts";

export type ConfiguredTask = {
  task_id: string;
  task_version: string;
  task_class: string;
  allowed_data: string[];
  allowed_tiers: string[];
  prompt_version: string;
  spec_version: string;
};

// The legacy "provider + model" field. BEDROCK is deliberately included: it is
// a real hosted provider with a bearer key, unlike TypeSafe Jev which belongs
// only to the decision tier.
export const PROVIDERS = ["GEMINI", "DEEPSEEK", "OPENAI", "CUSTOM", "BEDROCK"] as const;
export type Provider = (typeof PROVIDERS)[number];

export const ALL_PROVIDERS = [...PROVIDERS, "TYPESAFE_JEV"] as const;
export type KeyProvider = (typeof ALL_PROVIDERS)[number];

export const TIER_NAMES = ["decision", "reasoning", "flash"] as const;
export type TierName = (typeof TIER_NAMES)[number];

export const DECISION_FALLBACKS = ["flash", "reasoning", "heuristic"] as const;
export type DecisionFallback = (typeof DECISION_FALLBACKS)[number];

export const DECISION_PREVIEWS = [
  "jev",
  "flash",
  "reasoning",
  "heuristic",
  "rules_only",
] as const;
export type DecisionPreview = (typeof DECISION_PREVIEWS)[number];

export type ApiKeyStatus = {
  provider: KeyProvider;
  configured: boolean;
  masked: string | null;
};

export type TierConfig = {
  provider: KeyProvider | null;
  model: string;
  temperature: number;
  max_tokens: number;
  timeout_ms: number;
  max_attempts: number;
  fallback_tier: TierName | null;
  configured: boolean;
};

export type AgentRoute = {
  tier: TierName;
  provider: KeyProvider | null;
  model: string | null;
};

export type DecisionPolicy = {
  min_choice_confidence: number;
  min_noul_probability: number;
  abstain_score_band: [number, number];
  llm_fallback_size_scale: number;
  heuristic_size_scale: number;
};

export type PipelineConfig = {
  debate_rounds: number;
  max_tool_rounds_per_agent: number;
  screener_min_score: number;
  final_decision_min_prob: number;
  execution_mode: string;
  decision_fallback_order: DecisionFallback[];
};

export type ModelsConfig = {
  tiers: Record<TierName, TierConfig>;
  agent_routing: Record<string, AgentRoute>;
  decision_policy: DecisionPolicy;
  pipeline: PipelineConfig;
  decision_route: string;
  configured_tiers: TierName[];
  decision_preview: DecisionPreview;
};

export type AppConfig = {
  provider: Provider;
  model_name: string;
  news_feed_url: string;
  news_memory_ttl_days: number;
  api_keys: ApiKeyStatus[];
  model_route_configured: boolean;
  updated_at: string | null;
  // Bedrock is regional; absent means the client default (us-east-1).
  bedrock_region: string | null;
};

export type SettingsOverview = {
  tasks: ConfiguredTask[];
  task_count: number;
  schedule_count: number;
  scheduler_running: boolean;
  model_usage_groups: number;
  provider_connected: boolean;
  api_keys_configured: boolean;
  agent_configuration_state: "not_configured" | "configured";
  config: AppConfig;
};

export const MAX_MODEL_NAME_CHARS = 200;
export const MIN_MEMORY_TTL_DAYS = 1;
export const MAX_MEMORY_TTL_DAYS = 3650;

function isStringList(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((item) => typeof item === "string" && item.length > 0)
  );
}

function parseProvider(value: unknown): Provider {
  if (typeof value !== "string" || !(PROVIDERS as readonly string[]).includes(value)) {
    throw new Error("Invalid provider");
  }
  return value as Provider;
}

function parseKeyProvider(value: unknown): KeyProvider {
  if (
    typeof value !== "string" ||
    !(ALL_PROVIDERS as readonly string[]).includes(value)
  ) {
    throw new Error("Invalid provider");
  }
  return value as KeyProvider;
}

function parseTierName(value: unknown): TierName {
  if (typeof value !== "string" || !(TIER_NAMES as readonly string[]).includes(value)) {
    throw new Error("Invalid tier");
  }
  return value as TierName;
}

function parseTask(value: unknown): ConfiguredTask {
  if (
    !isRecord(value) ||
    typeof value.task_id !== "string" ||
    value.task_id.length === 0 ||
    typeof value.task_version !== "string" ||
    value.task_version.length === 0 ||
    typeof value.task_class !== "string" ||
    value.task_class.length === 0 ||
    !isStringList(value.allowed_data) ||
    !isStringList(value.allowed_tiers) ||
    typeof value.prompt_version !== "string" ||
    value.prompt_version.length === 0 ||
    typeof value.spec_version !== "string" ||
    value.spec_version.length === 0
  ) {
    throw new Error("Invalid configured task");
  }
  return {
    task_id: value.task_id,
    task_version: value.task_version,
    task_class: value.task_class,
    allowed_data: value.allowed_data,
    allowed_tiers: value.allowed_tiers,
    prompt_version: value.prompt_version,
    spec_version: value.spec_version,
  };
}

function parseApiKey(value: unknown): ApiKeyStatus {
  if (
    !isRecord(value) ||
    typeof value.configured !== "boolean" ||
    (value.masked !== null && typeof value.masked !== "string")
  ) {
    throw new Error("Invalid API key status");
  }
  const provider = parseKeyProvider(value.provider);
  if (value.configured !== (typeof value.masked === "string")) {
    throw new Error("Invalid API key status");
  }
  return {
    provider,
    configured: value.configured,
    masked: value.masked as string | null,
  };
}

export function parseConfig(value: unknown): AppConfig {
  if (
    !isRecord(value) ||
    typeof value.model_name !== "string" ||
    typeof value.news_feed_url !== "string" ||
    !isCount(value.news_memory_ttl_days) ||
    value.news_memory_ttl_days < MIN_MEMORY_TTL_DAYS ||
    value.news_memory_ttl_days > MAX_MEMORY_TTL_DAYS ||
    !Array.isArray(value.api_keys) ||
    value.api_keys.length > ALL_PROVIDERS.length ||
    typeof value.model_route_configured !== "boolean" ||
    (value.updated_at !== null &&
      (typeof value.updated_at !== "string" ||
        !Number.isFinite(Date.parse(value.updated_at))))
  ) {
    throw new Error("Invalid application config");
  }
  const apiKeys = value.api_keys.map(parseApiKey);
  const seen = new Set<string>();
  for (const key of apiKeys) {
    if (seen.has(key.provider)) {
      throw new Error("Invalid application config");
    }
    seen.add(key.provider);
  }
  return {
    provider: parseProvider(value.provider),
    model_name: value.model_name,
    news_feed_url: value.news_feed_url,
    news_memory_ttl_days: value.news_memory_ttl_days,
    api_keys: apiKeys,
    model_route_configured: value.model_route_configured,
    updated_at: value.updated_at as string | null,
    bedrock_region:
      typeof value.bedrock_region === "string" && value.bedrock_region.length > 0
        ? value.bedrock_region
        : null,
  };
}

export function parseOverview(value: unknown): SettingsOverview {
  if (
    !isRecord(value) ||
    !Array.isArray(value.tasks) ||
    value.tasks.length > 50 ||
    !isCount(value.task_count) ||
    value.tasks.length > value.task_count ||
    !isCount(value.schedule_count) ||
    typeof value.scheduler_running !== "boolean" ||
    !isCount(value.model_usage_groups) ||
    typeof value.provider_connected !== "boolean" ||
    typeof value.api_keys_configured !== "boolean" ||
    (value.agent_configuration_state !== "not_configured" &&
      value.agent_configuration_state !== "configured") ||
    !("config" in value)
  ) {
    throw new Error("Invalid settings overview");
  }
  const config = parseConfig(value.config);
  if (value.provider_connected !== config.model_route_configured) {
    throw new Error("Invalid settings overview");
  }
  return {
    tasks: value.tasks.map(parseTask),
    task_count: value.task_count,
    schedule_count: value.schedule_count,
    scheduler_running: value.scheduler_running,
    model_usage_groups: value.model_usage_groups,
    provider_connected: value.provider_connected,
    api_keys_configured: value.api_keys_configured,
    agent_configuration_state: value.agent_configuration_state,
    config,
  };
}

function parseProbability(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    throw new Error(`Invalid ${label}`);
  }
  return value;
}

function parseTier(value: unknown): TierConfig {
  if (
    !isRecord(value) ||
    (value.provider !== null && typeof value.provider !== "string") ||
    typeof value.model !== "string" ||
    typeof value.temperature !== "number" ||
    !Number.isFinite(value.temperature) ||
    value.temperature < 0 ||
    value.temperature > 2 ||
    !isCount(value.max_tokens) ||
    value.max_tokens < 1 ||
    !isCount(value.timeout_ms) ||
    value.timeout_ms < 100 ||
    !isCount(value.max_attempts) ||
    value.max_attempts < 1 ||
    value.max_attempts > 3 ||
    (value.fallback_tier !== null && typeof value.fallback_tier !== "string") ||
    typeof value.configured !== "boolean"
  ) {
    throw new Error("Invalid tier configuration");
  }
  return {
    provider: value.provider === null ? null : parseKeyProvider(value.provider),
    model: value.model,
    temperature: value.temperature,
    max_tokens: value.max_tokens,
    timeout_ms: value.timeout_ms,
    max_attempts: value.max_attempts,
    fallback_tier:
      value.fallback_tier === null ? null : parseTierName(value.fallback_tier),
    configured: value.configured,
  };
}

function parseAgentRoute(value: unknown): AgentRoute {
  if (
    !isRecord(value) ||
    (value.provider !== null && typeof value.provider !== "string") ||
    (value.model !== null && typeof value.model !== "string")
  ) {
    throw new Error("Invalid agent route");
  }
  return {
    tier: parseTierName(value.tier),
    provider: value.provider === null ? null : parseKeyProvider(value.provider),
    model: value.model as string | null,
  };
}

function parseDecisionPolicy(value: unknown): DecisionPolicy {
  if (
    !isRecord(value) ||
    !Array.isArray(value.abstain_score_band) ||
    value.abstain_score_band.length !== 2 ||
    !isCount(value.abstain_score_band[0]) ||
    !isCount(value.abstain_score_band[1]) ||
    value.abstain_score_band[0] > value.abstain_score_band[1]
  ) {
    throw new Error("Invalid decision policy");
  }
  return {
    min_choice_confidence: parseProbability(
      value.min_choice_confidence,
      "minimum choice confidence",
    ),
    min_noul_probability: parseProbability(
      value.min_noul_probability,
      "minimum noul probability",
    ),
    abstain_score_band: [
      value.abstain_score_band[0],
      value.abstain_score_band[1],
    ],
    llm_fallback_size_scale: parseProbability(
      value.llm_fallback_size_scale,
      "LLM fallback size scale",
    ),
    heuristic_size_scale: parseProbability(
      value.heuristic_size_scale,
      "heuristic size scale",
    ),
  };
}

function parsePipeline(value: unknown): PipelineConfig {
  if (
    !isRecord(value) ||
    !isCount(value.debate_rounds) ||
    !isCount(value.max_tool_rounds_per_agent) ||
    typeof value.execution_mode !== "string" ||
    !Array.isArray(value.decision_fallback_order) ||
    value.decision_fallback_order.length === 0 ||
    value.decision_fallback_order.length > 3
  ) {
    throw new Error("Invalid pipeline configuration");
  }
  const order = value.decision_fallback_order.map((item) => {
    if (
      typeof item !== "string" ||
      !(DECISION_FALLBACKS as readonly string[]).includes(item)
    ) {
      throw new Error("Invalid pipeline configuration");
    }
    return item as DecisionFallback;
  });
  if (new Set(order).size !== order.length) {
    throw new Error("Invalid pipeline configuration");
  }
  return {
    debate_rounds: value.debate_rounds,
    max_tool_rounds_per_agent: value.max_tool_rounds_per_agent,
    screener_min_score: parseProbability(
      value.screener_min_score,
      "screener minimum score",
    ),
    final_decision_min_prob: parseProbability(
      value.final_decision_min_prob,
      "final decision minimum probability",
    ),
    execution_mode: value.execution_mode,
    decision_fallback_order: order,
  };
}

export function parseModelsConfig(value: unknown): ModelsConfig {
  if (
    !isRecord(value) ||
    !isRecord(value.tiers) ||
    !isRecord(value.agent_routing) ||
    !isRecord(value.decision_policy) ||
    !isRecord(value.pipeline) ||
    typeof value.decision_route !== "string" ||
    !Array.isArray(value.configured_tiers) ||
    typeof value.decision_preview !== "string" ||
    !(DECISION_PREVIEWS as readonly string[]).includes(value.decision_preview)
  ) {
    throw new Error("Invalid models configuration");
  }
  const tiers = {} as Record<TierName, TierConfig>;
  for (const name of TIER_NAMES) {
    const raw = value.tiers[name];
    if (raw === undefined) {
      throw new Error("Invalid models configuration");
    }
    tiers[name] = parseTier(raw);
  }
  const routing: Record<string, AgentRoute> = {};
  for (const [agentId, raw] of Object.entries(value.agent_routing)) {
    if (agentId.length === 0) {
      throw new Error("Invalid agent routing");
    }
    routing[agentId] = parseAgentRoute(raw);
  }
  const configured = value.configured_tiers.map((item) => parseTierName(item));
  return {
    tiers,
    agent_routing: routing,
    decision_policy: parseDecisionPolicy(value.decision_policy),
    pipeline: parsePipeline(value.pipeline),
    decision_route: value.decision_route,
    configured_tiers: configured,
    decision_preview: value.decision_preview as DecisionPreview,
  };
}
