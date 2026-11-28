import { isCount, isRecord, isTimestamp } from "./contracts.ts";

export const MIN_RETENTION_DAYS = 1;
export const MAX_RETENTION_DAYS = 3650;
export const MIN_FLUSH_MINUTES = 1;
export const MAX_FLUSH_MINUTES = 1440;

export const LOG_KINDS = ["llm_call", "decision", "system"] as const;
export type LogKind = (typeof LOG_KINDS)[number];

export const LOG_LEVELS = ["info", "warning", "error"] as const;
export type LogLevel = (typeof LOG_LEVELS)[number];

export type LogFile = {
  name: string;
  size_bytes: number;
  modified_at: string;
};

export type LogEntry = {
  ts: string;
  kind: LogKind;
  level: LogLevel;
  truncated: boolean;
  data: Record<string, unknown>;
};

export type LoggingStatus = {
  enabled: boolean;
  retention_days: number;
  flush_minutes: number;
  directory: string;
  pending_bytes: number;
  dropped: number;
  files: LogFile[];
  recent: LogEntry[];
};

export type LogFlushResult = {
  written: number;
  files: string[];
  pruned: string[];
  status: LoggingStatus;
};

function parseKind(value: unknown): LogKind {
  if (
    typeof value !== "string" ||
    !(LOG_KINDS as readonly string[]).includes(value)
  ) {
    throw new Error("Invalid log kind");
  }
  return value as LogKind;
}

function parseLevel(value: unknown): LogLevel {
  if (
    typeof value !== "string" ||
    !(LOG_LEVELS as readonly string[]).includes(value)
  ) {
    throw new Error("Invalid log level");
  }
  return value as LogLevel;
}

function parseLogFile(value: unknown): LogFile {
  if (
    !isRecord(value) ||
    typeof value.name !== "string" ||
    value.name.length === 0 ||
    !isCount(value.size_bytes) ||
    !isTimestamp(value.modified_at)
  ) {
    throw new Error("Invalid log file");
  }
  return {
    name: value.name,
    size_bytes: value.size_bytes,
    modified_at: value.modified_at,
  };
}

function parseLogEntry(value: unknown): LogEntry {
  if (
    !isRecord(value) ||
    !isTimestamp(value.ts) ||
    !isRecord(value.data) ||
    typeof value.truncated !== "boolean"
  ) {
    throw new Error("Invalid log entry");
  }
  return {
    ts: value.ts,
    kind: parseKind(value.kind),
    level: parseLevel(value.level),
    truncated: value.truncated,
    data: value.data,
  };
}

function parseStringList(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw new Error("Invalid string list");
  }
  return value;
}

export function parseLoggingStatus(value: unknown): LoggingStatus {
  if (
    !isRecord(value) ||
    typeof value.enabled !== "boolean" ||
    !isCount(value.retention_days) ||
    value.retention_days < MIN_RETENTION_DAYS ||
    value.retention_days > MAX_RETENTION_DAYS ||
    !isCount(value.flush_minutes) ||
    value.flush_minutes < MIN_FLUSH_MINUTES ||
    value.flush_minutes > MAX_FLUSH_MINUTES ||
    typeof value.directory !== "string" ||
    !isCount(value.pending_bytes) ||
    !isCount(value.dropped) ||
    !Array.isArray(value.files) ||
    value.files.length > 500 ||
    !Array.isArray(value.recent) ||
    value.recent.length > 200
  ) {
    throw new Error("Invalid logging status");
  }
  return {
    enabled: value.enabled,
    retention_days: value.retention_days,
    flush_minutes: value.flush_minutes,
    directory: value.directory,
    pending_bytes: value.pending_bytes,
    dropped: value.dropped,
    files: value.files.map(parseLogFile),
    recent: value.recent.map(parseLogEntry),
  };
}

export function parseLogFlushResult(value: unknown): LogFlushResult {
  if (
    !isRecord(value) ||
    !isCount(value.written) ||
    !("status" in value)
  ) {
    throw new Error("Invalid flush result");
  }
  return {
    written: value.written,
    files: parseStringList(value.files),
    pruned: parseStringList(value.pruned),
    status: parseLoggingStatus(value.status),
  };
}

export function logEntryHeadline(entry: LogEntry): string {
  const data = entry.data;
  if (entry.kind === "llm_call") {
    const provider = typeof data.provider === "string" ? data.provider : "model";
    const model = typeof data.model === "string" ? data.model : "";
    const task = typeof data.task_id === "string" ? data.task_id : "";
    const outcome = data.ok === false ? "failed" : "ok";
    return [provider, model, task, outcome].filter((part) => part !== "").join(" · ");
  }
  if (entry.kind === "decision") {
    const scope = typeof data.scope === "string" ? data.scope : "decision";
    const symbol = typeof data.symbol === "string" ? ` ${data.symbol}` : "";
    const action =
      typeof data.action === "string"
        ? ` ${data.action}`
        : typeof data.status === "string"
          ? ` ${data.status}`
          : "";
    return `${scope}${symbol}${action}`;
  }
  return typeof data.message === "string" ? data.message : "system";
}

export function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 ** 2) {
    return `${(value / 1024).toFixed(1)} KiB`;
  }
  if (value < 1024 ** 3) {
    return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  }
  return `${(value / 1024 ** 3).toFixed(2)} GiB`;
}
