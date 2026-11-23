export function parseInterval(value: Record<string, unknown>): {
  interval_minutes: number | null;
  window_end: string | null;
} {
  if (value.interval_minutes === null && value.window_end === null) {
    return { interval_minutes: null, window_end: null };
  }
  if (
    typeof value.interval_minutes !== "number" ||
    !Number.isSafeInteger(value.interval_minutes) ||
    value.interval_minutes < 1 || value.interval_minutes > 1440 ||
    typeof value.window_end !== "string" || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value.window_end)
  ) throw new Error("Invalid schedule interval");
  return { interval_minutes: value.interval_minutes, window_end: value.window_end };
}

export function parseWeekdays(value: unknown): number[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 7) {
    throw new Error("Invalid schedule weekdays");
  }
  const days: number[] = [];
  for (const day of value) {
    if (
      typeof day !== "number" || !Number.isInteger(day) || day < 0 || day > 6 ||
      (days.length > 0 && day <= days[days.length - 1])
    ) throw new Error("Invalid schedule weekdays");
    days.push(day);
  }
  return days;
}
