type Day = {
  date: string;
  state: "UNCOVERED" | "OPEN" | "CLOSED";
  sessionClass: "REGULAR" | "SPECIAL" | "HOLIDAY" | null;
  opensAt: string | null;
  closesAt: string | null;
};
export type Result = { exchange: string; asOf: string; days: Day[] };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isTimestamp(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

export function dateMillis(value: string): number {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return NaN;
  const millis = Date.parse(`${value}T00:00:00.000Z`);
  return Number.isFinite(millis) && new Date(millis).toISOString().slice(0, 10) === value
    ? millis : NaN;
}

export function parseResult(value: unknown, exchange: string, start: string, end: string, asOf: string): Result {
  if (
    !isRecord(value) || value.exchange_code !== exchange || !isTimestamp(value.as_of) ||
    Date.parse(value.as_of) !== Date.parse(asOf) || !Array.isArray(value.days) ||
    value.days.length !== (dateMillis(end) - dateMillis(start)) / 86400000 + 1
  ) throw new Error("Invalid calendar coverage");
  const days = value.days.map((item: unknown, index: number): Day => {
    if (
      !isRecord(item) || typeof item.session_date !== "string" ||
      dateMillis(item.session_date) !== dateMillis(start) + index * 86400000 ||
      (item.state !== "UNCOVERED" && item.state !== "OPEN" && item.state !== "CLOSED")
    ) throw new Error("Invalid calendar date");
    if (item.state === "UNCOVERED") {
      if (
        item.session_class !== null || item.session_id !== null || item.opens_at !== null ||
        item.closes_at !== null || item.known_at !== null || item.reviewed_at !== null ||
        item.recorded_at !== null
      ) throw new Error("Invalid uncovered date");
      return { date: item.session_date, state: item.state, sessionClass: null, opensAt: null, closesAt: null };
    }
    if (
      (item.session_class !== "REGULAR" && item.session_class !== "SPECIAL" && item.session_class !== "HOLIDAY") ||
      (item.state === "OPEN" && item.session_class === "HOLIDAY") ||
      typeof item.session_id !== "string" || item.session_id.length === 0 ||
      !isTimestamp(item.known_at) || !isTimestamp(item.reviewed_at) || !isTimestamp(item.recorded_at) ||
      Date.parse(item.known_at) > Date.parse(asOf) || Date.parse(item.reviewed_at) > Date.parse(asOf) ||
      Date.parse(item.recorded_at) > Date.parse(asOf) ||
      (item.state === "OPEN"
        ? !isTimestamp(item.opens_at) || !isTimestamp(item.closes_at) || Date.parse(item.opens_at) >= Date.parse(item.closes_at)
        : item.opens_at !== null || item.closes_at !== null)
    ) throw new Error("Invalid calendar session");
    return {
      date: item.session_date,
      state: item.state,
      sessionClass: item.session_class,
      opensAt: typeof item.opens_at === "string" ? item.opens_at : null,
      closesAt: typeof item.closes_at === "string" ? item.closes_at : null,
    };
  });
  return { exchange, asOf, days };
}
