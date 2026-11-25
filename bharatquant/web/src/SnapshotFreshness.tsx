import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { formatAge, parseFreshness } from "./freshnessContract";
import type { FreshnessQuery, FreshnessResult } from "./freshnessContract";

type Status =
  | { state: "idle" | "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; query: FreshnessQuery; result: FreshnessResult };

function utc(value: string): string {
  return value.replace("T", " ").replace("Z", " UTC");
}

export default function SnapshotFreshness() {
  const [recordId, setRecordId] = useState("");
  const [delayed, setDelayed] = useState("");
  const [critical, setCritical] = useState("");
  const [mode, setMode] = useState<"WARN" | "BLOCK" | "ALLOW">("WARN");
  const [status, setStatus] = useState<Status>({ state: "idle" });
  const active = useRef<AbortController | null>(null);

  useEffect(() => () => active.current?.abort(), []);

  function invalidate() {
    active.current?.abort();
    setStatus({ state: "idle" });
  }

  async function check(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    active.current?.abort();
    const delayedSeconds = Number(delayed);
    const criticalSeconds = Number(critical);
    if (
      !/^[A-Za-z0-9_-]{1,128}$/.test(recordId) ||
      !/^[0-9]{1,8}$/.test(delayed) || !/^[0-9]{1,8}$/.test(critical) ||
      delayedSeconds >= criticalSeconds || criticalSeconds > 31536000
    ) {
      setStatus({ state: "error", message: "Enter a record ID and ordered whole-second thresholds within one year." });
      return;
    }
    const query: FreshnessQuery = {
      recordId, asOf: new Date().toISOString(), delayed: delayedSeconds, critical: criticalSeconds, mode,
    };
    const params = new URLSearchParams({
      record_id: query.recordId, as_of: query.asOf, delayed_after_seconds: delayed,
      critical_after_seconds: critical, delayed_mode: query.mode,
    });
    const controller = new AbortController();
    active.current = controller;
    setStatus({ state: "loading" });
    try {
      const response = await fetch(`/api/v1/data/snapshot-freshness?${params}`, {
        cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error("Snapshot freshness unavailable");
      const value: unknown = await response.json();
      const result = parseFreshness(value, query);
      if (!controller.signal.aborted) setStatus({ state: "ready", query, result });
    } catch {
      if (!controller.signal.aborted) {
        setStatus({ state: "error", message: "Snapshot unavailable or invalid. Check the ID, thresholds and your session." });
      }
    }
  }

  return (
    <section className="data-panel calendar-panel" aria-labelledby="freshness-title">
      <h2 id="freshness-title">Snapshot freshness diagnostic</h2>
      <p>Inspect a retained snapshot ID using thresholds you choose. Source, quality and timestamps are assertions; this is not trading readiness.</p>
      <form className="calendar-form" onSubmit={(event) => void check(event)}>
        <label htmlFor="snapshot-id">Snapshot record ID</label>
        <input id="snapshot-id" value={recordId} onChange={(event) => { invalidate(); setRecordId(event.target.value); }} maxLength={128} required />
        <label htmlFor="snapshot-delayed">Delayed after (seconds)</label>
        <input id="snapshot-delayed" inputMode="numeric" value={delayed} onChange={(event) => { invalidate(); setDelayed(event.target.value); }} maxLength={8} required />
        <label htmlFor="snapshot-critical">Critical after (seconds)</label>
        <input id="snapshot-critical" inputMode="numeric" value={critical} onChange={(event) => { invalidate(); setCritical(event.target.value); }} maxLength={8} required />
        <label htmlFor="snapshot-mode">Delayed-data mode</label>
        <select id="snapshot-mode" value={mode} onChange={(event) => { invalidate(); const value = event.target.value; if (value === "WARN" || value === "BLOCK" || value === "ALLOW") setMode(value); }}>
          <option value="WARN">Warn</option>
          <option value="BLOCK">Block</option>
          <option value="ALLOW">Allow (still shown as delayed)</option>
        </select>
        <button type="submit" className="auth-button" disabled={status.state === "loading"}>Inspect snapshot</button>
      </form>
      {status.state === "loading" && <p role="status">Inspecting snapshot...</p>}
      {status.state === "error" && <p role="alert">{status.message}</p>}
      {status.state === "ready" && (
        <div role="status">
          <p>Result: {status.result.status} · Source: {status.result.sourceId} · Quality: {status.result.quality}</p>
          <p>Age: {status.result.ageMicroseconds === null ? "unknown" : `${formatAge(status.result.ageMicroseconds)} seconds`}</p>
          <p>As of <time dateTime={status.query.asOf}>{utc(status.query.asOf)}</time> (device clock).</p>
          <p>Event: {status.result.eventAt === null ? "unknown" : <time dateTime={status.result.eventAt}>{utc(status.result.eventAt)}</time>}</p>
          <p>Known: <time dateTime={status.result.knownAt}>{utc(status.result.knownAt)}</time> · Recorded: <time dateTime={status.result.recordedAt}>{utc(status.result.recordedAt)}</time></p>
          <p>Thresholds: delayed after {status.query.delayed} seconds ({status.query.mode}); critical after {status.query.critical} seconds. Equality does not exceed a threshold.</p>
        </div>
      )}
    </section>
  );
}
