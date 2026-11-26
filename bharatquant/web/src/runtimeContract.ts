import { isCount, isRecord, isTimestamp } from "./contracts.ts";

export type RuntimeStatus = {
  status: "ok" | "degraded";
  database_bytes: number;
  wal_bytes: number;
  free_disk_bytes: number;
  migration_count: number;
  integrity: string;
  jobs: {
    pending: number;
    running: number;
    failed: number;
    stale: number;
    failed_24h: number;
    last_event_at: string | null;
  };
  counts: {
    reports: number;
    research_notes: number;
    paper_orders: number;
    predictions: number;
  };
  issues: string[];
  checked_at: string;
};

export function parseRuntimeStatus(value: unknown): RuntimeStatus {
  if (
    !isRecord(value) ||
    (value.status !== "ok" && value.status !== "degraded") ||
    !isCount(value.database_bytes) ||
    !isCount(value.wal_bytes) ||
    !isCount(value.free_disk_bytes) ||
    !isCount(value.migration_count) ||
    typeof value.integrity !== "string" ||
    !isRecord(value.jobs) ||
    !isRecord(value.counts) ||
    !Array.isArray(value.issues) ||
    !value.issues.every((item) => typeof item === "string") ||
    !isTimestamp(value.checked_at)
  ) {
    throw new Error("Invalid runtime status");
  }
  const jobs = value.jobs;
  const counts = value.counts;
  for (const key of ["pending", "running", "failed", "stale", "failed_24h"]) {
    if (!isCount(jobs[key])) {
      throw new Error("Invalid runtime job count");
    }
  }
  for (const key of ["reports", "research_notes", "paper_orders", "predictions"]) {
    if (!isCount(counts[key])) {
      throw new Error("Invalid runtime table count");
    }
  }
  if (jobs.last_event_at !== null && !isTimestamp(jobs.last_event_at)) {
    throw new Error("Invalid runtime event time");
  }
  return {
    status: value.status,
    database_bytes: value.database_bytes,
    wal_bytes: value.wal_bytes,
    free_disk_bytes: value.free_disk_bytes,
    migration_count: value.migration_count,
    integrity: value.integrity,
    jobs: {
      pending: jobs.pending as number,
      running: jobs.running as number,
      failed: jobs.failed as number,
      stale: jobs.stale as number,
      failed_24h: jobs.failed_24h as number,
      last_event_at: jobs.last_event_at as string | null,
    },
    counts: {
      reports: counts.reports as number,
      research_notes: counts.research_notes as number,
      paper_orders: counts.paper_orders as number,
      predictions: counts.predictions as number,
    },
    issues: value.issues,
    checked_at: value.checked_at,
  };
}
