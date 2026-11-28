import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import TableScroll from "./TableScroll";
import { formatUtc } from "./contracts.ts";
import {
  MAX_FLUSH_MINUTES,
  MAX_RETENTION_DAYS,
  MIN_FLUSH_MINUTES,
  MIN_RETENTION_DAYS,
  formatBytes,
  logEntryHeadline,
  parseLogFlushResult,
  parseLoggingStatus,
} from "./loggingContract.ts";
import type { LogEntry, LoggingStatus } from "./loggingContract.ts";

type LoadState =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; status: LoggingStatus };

type SaveState =
  | { state: "idle" | "saving" }
  | { state: "saved"; message: string }
  | { state: "failed"; message: string };

async function detailMessage(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    return typeof body.detail === "string" ? body.detail : fallback;
  } catch {
    return fallback;
  }
}

function prettyFields(data: Record<string, unknown>): string {
  const omitted = new Set(["system", "prompt", "response", "reasoning"]);
  const rest: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(data)) {
    if (!omitted.has(key)) {
      rest[key] = value;
    }
  }
  return Object.keys(rest).length === 0
    ? "(no other fields)"
    : JSON.stringify(rest, null, 2);
}

function EntryDetail({ entry }: { entry: LogEntry }) {
  const data = entry.data;
  return (
    <details className="log-entry">
      <summary>
        <span className="log-time">{formatUtc(entry.ts)}</span>
        <span className="log-kind">{entry.kind}</span>
        <span>{logEntryHeadline(entry)}</span>
        {entry.truncated && (
          <span className="status-badge status-badge--provisional">TRUNCATED</span>
        )}
      </summary>
      {typeof data.system === "string" && data.system !== "" && (
        <>
          <h4>System</h4>
          <pre>{data.system}</pre>
        </>
      )}
      {typeof data.prompt === "string" && data.prompt !== "" && (
        <>
          <h4>Prompt</h4>
          <pre>{data.prompt}</pre>
        </>
      )}
      {typeof data.response === "string" && data.response !== "" && (
        <>
          <h4>Response</h4>
          <pre>{data.response}</pre>
        </>
      )}
      {typeof data.reasoning === "string" && data.reasoning !== "" && (
        <>
          <h4>Reasoning</h4>
          <pre>{data.reasoning}</pre>
        </>
      )}
      <h4>Fields</h4>
      <pre>{prettyFields(data)}</pre>
    </details>
  );
}

export default function LoggingPanel({ csrfToken }: { csrfToken: string }) {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [enabled, setEnabled] = useState(true);
  const [retention, setRetention] = useState("14");
  const [flushMinutes, setFlushMinutes] = useState("15");
  const [save, setSave] = useState<SaveState>({ state: "idle" });

  function apply(status: LoggingStatus) {
    setLoad({ state: "ready", status });
    setEnabled(status.enabled);
    setRetention(String(status.retention_days));
    setFlushMinutes(String(status.flush_minutes));
  }

  useEffect(() => {
    const controller = new AbortController();
    async function loadStatus() {
      try {
        const response = await fetch("/api/v1/logs", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Logging status unavailable");
        }
        apply(parseLoggingStatus(await response.json()));
      } catch {
        if (!controller.signal.aborted) {
          setLoad({ state: "error" });
        }
      }
    }
    void loadStatus();
    return () => controller.abort();
  }, []);

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const retentionDays = Number(retention);
    const minutes = Number(flushMinutes);
    if (
      !Number.isInteger(retentionDays) ||
      retentionDays < MIN_RETENTION_DAYS ||
      retentionDays > MAX_RETENTION_DAYS
    ) {
      setSave({
        state: "failed",
        message: `Retention must be ${MIN_RETENTION_DAYS}-${MAX_RETENTION_DAYS} days.`,
      });
      return;
    }
    if (
      !Number.isInteger(minutes) ||
      minutes < MIN_FLUSH_MINUTES ||
      minutes > MAX_FLUSH_MINUTES
    ) {
      setSave({
        state: "failed",
        message: `Flush interval must be ${MIN_FLUSH_MINUTES}-${MAX_FLUSH_MINUTES} minutes.`,
      });
      return;
    }
    setSave({ state: "saving" });
    try {
      const response = await fetch("/api/v1/logs", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          enabled,
          retention_days: retentionDays,
          flush_minutes: minutes,
        }),
      });
      if (!response.ok) {
        setSave({
          state: "failed",
          message: await detailMessage(response, "Saving logging settings failed."),
        });
        return;
      }
      apply(parseLoggingStatus(await response.json()));
      setSave({ state: "saved", message: "Logging settings saved." });
    } catch {
      setSave({
        state: "failed",
        message: "Saving logging settings failed. Check the local service.",
      });
    }
  }

  async function flushNow() {
    setSave({ state: "saving" });
    try {
      const response = await fetch("/api/v1/logs/flush", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (!response.ok) {
        setSave({
          state: "failed",
          message: await detailMessage(response, "Flushing logs failed."),
        });
        return;
      }
      const result = parseLogFlushResult(await response.json());
      apply(result.status);
      const noun = result.written === 1 ? "entry" : "entries";
      setSave({
        state: "saved",
        message: `Flushed ${result.written} ${noun}.`,
      });
    } catch {
      setSave({
        state: "failed",
        message: "Flushing logs failed. Check the local service.",
      });
    }
  }

  if (load.state === "loading") {
    return (
      <section className="settings-panel" aria-label="Operational logs">
        <p role="status">Loading logging status...</p>
      </section>
    );
  }
  if (load.state === "error") {
    return (
      <section className="settings-panel" aria-label="Operational logs">
        <p role="alert">Logging status is unavailable.</p>
      </section>
    );
  }

  const status = load.status;
  const busy = save.state === "saving";

  return (
    <section className="settings-panel" aria-labelledby="logging-title">
      <h2 id="logging-title">Operational logs</h2>
      <p className="panel-note">
        Every LLM call and trading decision is buffered, then written to a dated
        file every {status.flush_minutes} minute
        {status.flush_minutes === 1 ? "" : "s"}. Full prompts and responses are
        stored in plain text under the app data directory. Files older than the
        retention window are deleted on each flush.
      </p>

      {save.state === "failed" && (
        <p className="auth-error" role="alert">
          {save.message}
        </p>
      )}
      {save.state === "saved" && (
        <p className="config-feedback" role="status">
          {save.message}
        </p>
      )}

      <form className="config-form" onSubmit={(event) => void saveSettings(event)}>
        <div className="models-grid models-grid--compact">
          <label className="log-toggle" htmlFor="log-enabled">
            <input
              id="log-enabled"
              type="checkbox"
              checked={enabled}
              onChange={(event) => setEnabled(event.target.checked)}
            />
            Record new entries
          </label>
          <label htmlFor="log-retention">
            Retention (days)
            <input
              id="log-retention"
              type="number"
              min={MIN_RETENTION_DAYS}
              max={MAX_RETENTION_DAYS}
              value={retention}
              onChange={(event) => setRetention(event.target.value)}
            />
          </label>
          <label htmlFor="log-flush">
            Flush interval (minutes)
            <input
              id="log-flush"
              type="number"
              min={MIN_FLUSH_MINUTES}
              max={MAX_FLUSH_MINUTES}
              value={flushMinutes}
              onChange={(event) => setFlushMinutes(event.target.value)}
            />
          </label>
        </div>
        <button type="submit" disabled={busy}>
          {busy ? "Saving..." : "Save logging settings"}
        </button>
      </form>

      <div className="log-actions">
        <button type="button" onClick={() => void flushNow()} disabled={busy}>
          Flush now
        </button>
      </div>

      <dl className="settings-facts">
        <div>
          <dt>Directory</dt>
          <dd>{status.directory === "" ? "Not configured" : status.directory}</dd>
        </div>
        <div>
          <dt>Pending buffer</dt>
          <dd>{formatBytes(status.pending_bytes)}</dd>
        </div>
        <div>
          <dt>Dropped entries</dt>
          <dd>{status.dropped}</dd>
        </div>
        <div>
          <dt>Files / retention</dt>
          <dd>
            {status.files.length} file{status.files.length === 1 ? "" : "s"} ·{" "}
            {status.retention_days} days
          </dd>
        </div>
      </dl>

      <h3>Log files</h3>
      {status.files.length === 0 ? (
        <p className="panel-note">
          No flushed files yet. Buffered entries are written on the next flush.
        </p>
      ) : (
        <TableScroll label="Log files">
          <table className="judgment-table log-files">
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">Size</th>
                <th scope="col">Modified</th>
              </tr>
            </thead>
            <tbody>
              {status.files.map((file) => (
                <tr key={file.name}>
                  <th scope="row">{file.name}</th>
                  <td>{formatBytes(file.size_bytes)}</td>
                  <td>{formatUtc(file.modified_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}

      <h3>Recent entries</h3>
      {status.recent.length === 0 ? (
        <p className="panel-note">No entries buffered or written yet.</p>
      ) : (
        <div className="log-entries">
          {status.recent.map((entry, index) => (
            <EntryDetail key={`${entry.ts}-${index}`} entry={entry} />
          ))}
        </div>
      )}
    </section>
  );
}
