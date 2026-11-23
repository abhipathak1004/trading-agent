import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { dateMillis, parseResult } from "./calendarContract";
import type { Result } from "./calendarContract";

type Status =
  | { state: "idle" | "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; result: Result };

function utc(value: string): string {
  return new Date(value).toISOString().replace("T", " ").replace("Z", " UTC");
}

export default function CalendarCoverage() {
  const [exchange, setExchange] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [status, setStatus] = useState<Status>({ state: "idle" });
  const active = useRef<AbortController | null>(null);

  useEffect(() => () => active.current?.abort(), []);

  async function check(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    active.current?.abort();
    const span = (dateMillis(end) - dateMillis(start)) / 86400000 + 1;
    if (!/^[A-Z][A-Z0-9._-]{0,15}$/.test(exchange) || !Number.isInteger(span) || span < 1 || span > 31) {
      setStatus({ state: "error", message: "Use a valid exchange code and an ordered range of 1–31 dates." });
      return;
    }
    const controller = new AbortController();
    active.current = controller;
    const asOf = new Date().toISOString();
    const query = new URLSearchParams({ exchange_code: exchange, start, end, as_of: asOf });
    setStatus({ state: "loading" });
    try {
      const response = await fetch(`/api/v1/calendar/coverage?${query}`, {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("Calendar coverage unavailable");
      const value: unknown = await response.json();
      setStatus({ state: "ready", result: parseResult(value, exchange, start, end, asOf) });
    } catch {
      if (!controller.signal.aborted) {
        setStatus({ state: "error", message: "Calendar coverage is unavailable. Check your inputs and try again." });
      }
    }
  }

  return (
    <section className="data-panel calendar-panel" aria-labelledby="calendar-title">
      <h2 id="calendar-title">Calendar coverage</h2>
      <p>Uncovered dates are unknown, not open or closed. Owner review is not independent verification.</p>
      <form className="calendar-form" onSubmit={(event) => void check(event)}>
        <label htmlFor="calendar-exchange">Exchange code</label>
        <input id="calendar-exchange" value={exchange} onChange={(event) => setExchange(event.target.value)} maxLength={16} required />
        <label htmlFor="calendar-start">First date</label>
        <input id="calendar-start" type="date" value={start} onChange={(event) => setStart(event.target.value)} required />
        <label htmlFor="calendar-end">Last date</label>
        <input id="calendar-end" type="date" value={end} onChange={(event) => setEnd(event.target.value)} required />
        <button type="submit" className="auth-button" disabled={status.state === "loading"}>Check coverage</button>
      </form>
      {status.state === "loading" && <p role="status">Checking calendar coverage...</p>}
      {status.state === "error" && <p role="alert">{status.message}</p>}
      {status.state === "ready" && (
        <>
          <p>
            {status.result.exchange}, as of <time dateTime={status.result.asOf}>{utc(status.result.asOf)}</time>
            {" "}(device clock).
          </p>
          <ul>
            {status.result.days.map((day) => (
              <li key={day.date}>
                <time dateTime={day.date}>{day.date}</time>: {day.state === "UNCOVERED" ? "Uncovered" : `${day.state} (${day.sessionClass})`}
                {day.opensAt !== null && day.closesAt !== null && (
                  <> · <time dateTime={day.opensAt}>{utc(day.opensAt)}</time> to <time dateTime={day.closesAt}>{utc(day.closesAt)}</time></>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
