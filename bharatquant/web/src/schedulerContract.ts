import { isCount, isRecord, isTimestamp } from "./contracts.ts";

export type JobDefinition = {
  job_id: string;
  label: string;
  description: string;
  resource_class: string;
  timezone: string;
  local_time: string;
  window_start: string;
  window_end: string;
  enabled_by_default: boolean;
  max_attempts: number;
  wake_policy: string;
};

export type ScheduleConfig = {
  schedule_id: string;
  job_id: string;
  enabled: boolean;
  timezone: string;
  local_time: string;
  window_start: string;
  window_end: string;
  wake_policy: string;
  wake_within_seconds: number | null;
  max_attempts: number;
  interval_minutes: number | null;
  weekdays: number[];
  dependencies: string[];
  version: number;
  next_occurrences: string[];
};

export type ScheduleConfigList = {
  version: number;
  schedules: ScheduleConfig[];
};

export type ScheduledJobRun = {
  run_id: string;
  job_id: string;
  occurrence_key: string;
  scheduled_at: string;
  status: string;
  attempts: number;
  max_attempts: number;
  failure_code: string | null;
};

function timeValue(value: unknown): string {
  if (typeof value !== "string" || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value)) {
    throw new Error("Invalid schedule time");
  }
  return value;
}

export function parseJobCatalogue(value: unknown): JobDefinition[] {
  if (!isRecord(value) || !Array.isArray(value.jobs)) {
    throw new Error("Invalid job catalogue");
  }
  return value.jobs.map((item) => {
    if (
      !isRecord(item) ||
      typeof item.job_id !== "string" ||
      typeof item.label !== "string" ||
      typeof item.description !== "string" ||
      typeof item.resource_class !== "string" ||
      typeof item.timezone !== "string" ||
      typeof item.enabled_by_default !== "boolean" ||
      !isCount(item.max_attempts) ||
      typeof item.wake_policy !== "string"
    ) {
      throw new Error("Invalid job definition");
    }
    return {
      job_id: item.job_id,
      label: item.label,
      description: item.description,
      resource_class: item.resource_class,
      timezone: item.timezone,
      local_time: timeValue(item.local_time),
      window_start: timeValue(item.window_start),
      window_end: timeValue(item.window_end),
      enabled_by_default: item.enabled_by_default,
      max_attempts: item.max_attempts,
      wake_policy: item.wake_policy,
    };
  });
}

function parseSchedule(value: unknown): ScheduleConfig {
  if (
    !isRecord(value) ||
    typeof value.schedule_id !== "string" ||
    typeof value.job_id !== "string" ||
    typeof value.enabled !== "boolean" ||
    typeof value.timezone !== "string" ||
    typeof value.wake_policy !== "string" ||
    !isCount(value.max_attempts) ||
    !isCount(value.version) ||
    !Array.isArray(value.weekdays) ||
    !value.weekdays.every((item) => isCount(item) && item <= 6) ||
    !Array.isArray(value.dependencies) ||
    !value.dependencies.every((item) => typeof item === "string") ||
    !Array.isArray(value.next_occurrences) ||
    !value.next_occurrences.every(isTimestamp) ||
    (value.wake_within_seconds !== null &&
      !isCount(value.wake_within_seconds)) ||
    (value.interval_minutes !== null && !isCount(value.interval_minutes))
  ) {
    throw new Error("Invalid schedule");
  }
  return {
    schedule_id: value.schedule_id,
    job_id: value.job_id,
    enabled: value.enabled,
    timezone: value.timezone,
    local_time: timeValue(value.local_time),
    window_start: timeValue(value.window_start),
    window_end: timeValue(value.window_end),
    wake_policy: value.wake_policy,
    wake_within_seconds: value.wake_within_seconds as number | null,
    max_attempts: value.max_attempts,
    interval_minutes: value.interval_minutes as number | null,
    weekdays: value.weekdays as number[],
    dependencies: value.dependencies as string[],
    version: value.version,
    next_occurrences: value.next_occurrences as string[],
  };
}

export function parseScheduleList(value: unknown): ScheduleConfigList {
  if (!isRecord(value) || !isCount(value.version) || !Array.isArray(value.schedules)) {
    throw new Error("Invalid schedule list");
  }
  return {
    version: value.version,
    schedules: value.schedules.map(parseSchedule),
  };
}

export function parseScheduleConfig(value: unknown): ScheduleConfig {
  return parseSchedule(value);
}

export function parseJobRun(value: unknown): ScheduledJobRun {
  if (
    !isRecord(value) ||
    typeof value.run_id !== "string" ||
    typeof value.job_id !== "string" ||
    typeof value.occurrence_key !== "string" ||
    !isTimestamp(value.scheduled_at) ||
    typeof value.status !== "string" ||
    !isCount(value.attempts) ||
    !isCount(value.max_attempts) ||
    (typeof value.failure_code !== "string" && value.failure_code !== null)
  ) {
    throw new Error("Invalid job run");
  }
  return {
    run_id: value.run_id,
    job_id: value.job_id,
    occurrence_key: value.occurrence_key,
    scheduled_at: value.scheduled_at,
    status: value.status,
    attempts: value.attempts,
    max_attempts: value.max_attempts,
    failure_code: value.failure_code as string | null,
  };
}
