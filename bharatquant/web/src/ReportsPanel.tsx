import { useEffect, useState } from "react";

import { isRecord } from "./contracts.ts";
import { parseReport, parseReports } from "./reportContract.ts";
import type { Report } from "./reportContract.ts";

type State =
  | { state: "loading" | "error" }
  | { state: "ready"; reports: Report[] };

function rows(report: Report, key: "predictions" | "outcomes") {
  const value = report.body[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(isRecord).map((item) => ({
    kind: typeof item.kind === "string" ? item.kind : "",
    target: typeof item.target === "string" ? item.target : "",
    statement: typeof item.statement === "string" ? item.statement : "",
    probability:
      typeof item.probability === "number" ? item.probability : null,
    occurred: typeof item.occurred === "boolean" ? item.occurred : null,
  }));
}

function download(report: Report) {
  const url = URL.createObjectURL(
    new Blob([report.body_md], { type: "text/markdown;charset=utf-8" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `${report.report_type}-${report.report_date}.md`;
  link.click();
  URL.revokeObjectURL(url);
}

export default function ReportsPanel({ csrfToken }: { csrfToken: string }) {
  const [load, setLoad] = useState<State>({ state: "loading" });
  const [selected, setSelected] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function loadReports() {
      try {
        const response = await fetch("/api/v1/reports?limit=30", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Reports unavailable");
        }
        setLoad({ state: "ready", reports: parseReports(await response.json()) });
      } catch {
        if (!controller.signal.aborted) {
          setLoad({ state: "error" });
        }
      }
    }
    void loadReports();
    return () => controller.abort();
  }, [version]);

  async function run(type: "morning" | "evening") {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`/api/v1/reports/${type}`, {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (!response.ok) {
        let text = "The report could not be generated.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            text = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setMessage(text);
        return;
      }
      const report = parseReport(await response.json());
      setSelected(report);
      setMessage(`${report.title} saved.`);
      setVersion((value) => value + 1);
    } catch {
      setMessage("The report could not be generated.");
    } finally {
      setBusy(false);
    }
  }

  async function open(report: Report) {
    try {
      const response = await fetch(`/api/v1/reports/${report.report_id}`, {
        cache: "no-store",
      });
      if (response.ok) {
        setSelected(parseReport(await response.json()));
      }
    } catch {
      setMessage("The report could not be loaded.");
    }
  }

  return (
    <section className="settings-panel" aria-labelledby="reports-title">
      <h2 id="reports-title">Reports</h2>
      <p className="panel-note">
        Morning reports freeze their predictions before the session; evening
        reports resolve every due prediction and score it. Reports and predictions
        are append-only. No report places an order.
      </p>
      <div className="strategy-examples">
        <button
          type="button"
          className="auth-button"
          disabled={busy}
          onClick={() => void run("morning")}
        >
          {busy ? "Working..." : "Run morning report"}
        </button>
        <button
          type="button"
          className="text-button"
          disabled={busy}
          onClick={() => void run("evening")}
        >
          Run evening accuracy
        </button>
      </div>
      {message !== "" && (
        <p className="panel-note" role="status">{message}</p>
      )}
      {load.state === "loading" && <p role="status">Loading reports...</p>}
      {load.state === "error" && <p role="alert">Reports are unavailable.</p>}
      {load.state === "ready" && load.reports.length === 0 && (
        <p className="panel-note">No reports saved yet.</p>
      )}
      {load.state === "ready" && load.reports.length > 0 && (
        <table className="judgment-table">
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col">Type</th>
              <th scope="col">Status</th>
              <th scope="col">Title</th>
            </tr>
          </thead>
          <tbody>
            {load.reports.map((report) => (
              <tr key={report.report_id}>
                <th scope="row">{report.report_date}</th>
                <td>{report.report_type}</td>
                <td>{report.status}</td>
                <td>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => void open(report)}
                  >
                    {report.title}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {selected !== null && (
        <article className="strategy-result">
          <h3>{selected.title}</h3>
          <p className="prompt-meta">
            {selected.report_date} · {selected.status} · hash{" "}
            {selected.content_hash}
          </p>
          <div className="strategy-examples">
            <button
              type="button"
              className="text-button"
              onClick={() => download(selected)}
            >
              Download Markdown
            </button>
          </div>
          {rows(selected, "predictions").length > 0 && (
            <table className="judgment-table">
              <caption>Frozen predictions</caption>
              <thead>
                <tr>
                  <th scope="col">Kind</th>
                  <th scope="col">Target</th>
                  <th scope="col">Probability</th>
                  <th scope="col">Statement</th>
                </tr>
              </thead>
              <tbody>
                {rows(selected, "predictions").map((item, index) => (
                  <tr key={`${item.kind}-${index}`}>
                    <th scope="row">{item.kind}</th>
                    <td>{item.target}</td>
                    <td>
                      {item.probability === null
                        ? "n/a"
                        : `${Math.round(item.probability * 100)}%`}
                    </td>
                    <td>{item.statement}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {rows(selected, "outcomes").length > 0 && (
            <table className="judgment-table">
              <caption>Resolved outcomes</caption>
              <thead>
                <tr>
                  <th scope="col">Kind</th>
                  <th scope="col">Target</th>
                  <th scope="col">Probability</th>
                  <th scope="col">Result</th>
                </tr>
              </thead>
              <tbody>
                {rows(selected, "outcomes").map((item, index) => (
                  <tr key={`${item.kind}-${index}`}>
                    <th scope="row">{item.kind}</th>
                    <td>{item.target}</td>
                    <td>
                      {item.probability === null
                        ? "n/a"
                        : `${Math.round(item.probability * 100)}%`}
                    </td>
                    <td>{item.occurred === null ? "n/a" : item.occurred ? "HIT" : "MISS"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <pre className="working-memory-json">{selected.body_md}</pre>
        </article>
      )}
    </section>
  );
}
