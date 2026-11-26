import { isCount, isRecord } from "./contracts.ts";

export type MemorySchedule = {
  configured: boolean;
  enabled: boolean;
  schedule_id: string | null;
  version: number | null;
  local_time: string | null;
  timezone: string | null;
};

export function parseMemorySchedule(value: unknown): MemorySchedule {
  if (!isRecord(value) || typeof value.configured !== "boolean" ||
      typeof value.enabled !== "boolean") {
    throw new Error("Invalid memory schedule");
  }
  if (typeof value.schedule_id !== "string" && value.schedule_id !== null) {
    throw new Error("Invalid memory schedule");
  }
  if (value.version !== null && !isCount(value.version)) {
    throw new Error("Invalid memory schedule");
  }
  if (typeof value.local_time !== "string" && value.local_time !== null) {
    throw new Error("Invalid memory schedule");
  }
  if (typeof value.timezone !== "string" && value.timezone !== null) {
    throw new Error("Invalid memory schedule");
  }
  return {
    configured: value.configured,
    enabled: value.enabled,
    schedule_id: value.schedule_id as string | null,
    version: value.version as number | null,
    local_time: value.local_time as string | null,
    timezone: value.timezone as string | null,
  };
}
