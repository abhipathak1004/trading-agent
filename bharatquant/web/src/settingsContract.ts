export type ConfiguredTask = {
  task_id: string;
  task_version: string;
  task_class: string;
  allowed_data: string[];
  allowed_tiers: string[];
  prompt_version: string;
  spec_version: string;
};

export const PROVIDERS = ["GEMINI", "DEEPSEEK", "OPENAI", "CUSTOM"] as const;
export type Provider = (typeof PROVIDERS)[number];

export type ApiKeyStatus = {
  provider: Provider;
  configured: boolean;
  masked: string | null;
};

export type AppConfig = {
  provider: Provider;
  model_name: string;
  news_feed_url: string;
  news_memory_ttl_days: number;
  api_keys: ApiKeyStatus[];
  model_route_configured: boolean;
  updated_at: string | null;
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

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
  const provider = parseProvider(value.provider);
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
    value.api_keys.length > PROVIDERS.length ||
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
