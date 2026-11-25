export type ConfiguredTask = {
  task_id: string;
  task_version: string;
  task_class: string;
  allowed_data: string[];
  allowed_tiers: string[];
  prompt_version: string;
  spec_version: string;
};

export type SettingsOverview = {
  tasks: ConfiguredTask[];
  task_count: number;
  schedule_count: number;
  scheduler_running: boolean;
  model_usage_groups: number;
  provider_connected: false;
  api_keys_configured: false;
  agent_configuration_state: "not_configured";
};

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
    value.provider_connected !== false ||
    value.api_keys_configured !== false ||
    value.agent_configuration_state !== "not_configured"
  ) {
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
  };
}
