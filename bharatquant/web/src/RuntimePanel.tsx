import { useEffect, useState } from "react";

import { parseRuntimeStatus } from "./runtimeContract.ts";
import type { RuntimeStatus } from "./runtimeContract.ts";

function bytes(value: number): string {
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

export default function RuntimePanel() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const response = await fetch("/api/v1/system/status", { cache: "no-store" });
        if (!response.ok) {
          throw new Error("Runtime status unavailable.");
        }
        const parsed = parseRuntimeStatus(await response.json());
        if (active) {
          setStatus(parsed);
          setError("");
        }
      } catch {
        if (active) {
          setError("Runtime status unavailable.");
        }
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <section className="settings-panel" aria-labelledby="runtime-title">
      <h2 id="runtime-title">Runtime</h2>
      <p className="panel-note">
        SQLite/WAL health, job leases and storage headroom. A stale running job or
        a degraded disk is shown here before it becomes a silent failure.
      </p>
      {error !== "" && <p className="auth-error" role="alert">{error}</p>}
      {status === null && error === "" && <p role="status">Checking runtime...</p>}
      {status !== null && (
        <>
          <dl className="settings-facts">
            <div>
              <dt>Health</dt>
              <dd>{status.status.toUpperCase()} · {status.integrity}</dd>
            </div>
            <div>
              <dt>Database / WAL</dt>
              <dd>{bytes(status.database_bytes)} / {bytes(status.wal_bytes)}</dd>
            </div>
            <div>
              <dt>Free disk</dt>
              <dd>{bytes(status.free_disk_bytes)}</dd>
            </div>
            <div>
              <dt>Migrations</dt>
              <dd>{status.migration_count}</dd>
            </div>
            <div>
              <dt>Jobs pending / running / stale</dt>
              <dd>
                {status.jobs.pending} / {status.jobs.running} / {status.jobs.stale}
              </dd>
            </div>
            <div>
              <dt>Failed jobs (24h)</dt>
              <dd>{status.jobs.failed_24h}</dd>
            </div>
          </dl>
          <p className="panel-note">
            Reports {status.counts.reports} · notes {status.counts.research_notes} ·
            paper orders {status.counts.paper_orders} · predictions{" "}
            {status.counts.predictions}. Checked {status.checked_at.slice(0, 19)}Z.
          </p>
          {status.issues.length > 0 && (
            <ul className="panel-note" role="alert">
              {status.issues.map((issue) => (
                <li key={issue}>{issue}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
