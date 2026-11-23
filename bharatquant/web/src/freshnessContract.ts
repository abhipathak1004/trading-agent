export type FreshnessQuery = {
  recordId: string;
  asOf: string;
  delayed: number;
  critical: number;
  mode: "WARN" | "BLOCK" | "ALLOW";
};

export type FreshnessResult = {
  sourceId: string;
  quality: "UNASSESSED" | "ACCEPTABLE" | "DEGRADED" | "REJECTED";
  eventAt: string | null;
  knownAt: string;
  recordedAt: string;
  status: "FRESH" | "DELAYED_WARN" | "DELAYED_BLOCK" | "DELAYED_ALLOW" | "STALE_BLOCK" | "QUALITY_BLOCK" | "UNKNOWN_EVENT_TIME_BLOCK";
  ageMicroseconds: string | null;
};

function isTimestamp(value: unknown): value is string {
  return typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
    Number.isFinite(Date.parse(value));
}

function timestampMicroseconds(value: string): bigint {
  const fraction = /\.(\d{1,6})/.exec(value)?.[1] ?? "";
  return BigInt(Date.parse(value)) * 1000n + BigInt(fraction.padEnd(6, "0").slice(3));
}

export function parseFreshness(value: unknown, query: FreshnessQuery): FreshnessResult {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Invalid freshness response");
  }
  const data = value as Record<string, unknown>;
  if (
    data.record_id !== query.recordId || typeof data.source_id !== "string" || data.source_id.length === 0 ||
    (data.quality_status !== "UNASSESSED" && data.quality_status !== "ACCEPTABLE" &&
      data.quality_status !== "DEGRADED" && data.quality_status !== "REJECTED") ||
    !isTimestamp(data.as_of) || Date.parse(data.as_of) !== Date.parse(query.asOf) ||
    !isTimestamp(data.known_at) || !isTimestamp(data.recorded_at) ||
    Date.parse(data.known_at) > Date.parse(query.asOf) || Date.parse(data.recorded_at) > Date.parse(query.asOf) ||
    Date.parse(data.recorded_at) < Date.parse(data.known_at) ||
    data.delayed_after_seconds !== query.delayed || data.critical_after_seconds !== query.critical ||
    data.delayed_mode !== query.mode
  ) throw new Error("Invalid freshness metadata");
  let eventAt: string | null;
  let ageMicroseconds: string | null;
  let status: FreshnessResult["status"];
  if (data.event_at === null) {
    if (data.age_microseconds !== null || data.status !== "UNKNOWN_EVENT_TIME_BLOCK") {
      throw new Error("Invalid unknown event time");
    }
    eventAt = null;
    ageMicroseconds = null;
    status = "UNKNOWN_EVENT_TIME_BLOCK";
  } else {
    if (
      !isTimestamp(data.event_at) || Date.parse(data.event_at) > Date.parse(query.asOf) ||
      typeof data.age_microseconds !== "string" || !/^(0|[1-9][0-9]{0,19})$/.test(data.age_microseconds)
    ) throw new Error("Invalid snapshot age");
    const age = BigInt(data.age_microseconds);
    if (age !== timestampMicroseconds(data.as_of) - timestampMicroseconds(data.event_at)) {
      throw new Error("Snapshot age does not match timestamps");
    }
    const expected: FreshnessResult["status"] = data.quality_status !== "ACCEPTABLE" ? "QUALITY_BLOCK"
      : age > BigInt(query.critical) * 1000000n ? "STALE_BLOCK"
      : age > BigInt(query.delayed) * 1000000n ? `DELAYED_${query.mode}`
      : "FRESH";
    if (data.status !== expected) throw new Error("Invalid freshness status");
    eventAt = data.event_at;
    ageMicroseconds = data.age_microseconds;
    status = expected;
  }
  return {
    sourceId: data.source_id,
    quality: data.quality_status,
    eventAt,
    knownAt: data.known_at,
    recordedAt: data.recorded_at,
    status,
    ageMicroseconds,
  };
}

export function formatAge(microseconds: string): string {
  const value = BigInt(microseconds);
  const whole = value / 1000000n;
  const fraction = (value % 1000000n).toString().padStart(6, "0").replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : whole.toString();
}
