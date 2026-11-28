import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";

import TableScroll from "./TableScroll";
import { isRecord } from "./contracts.ts";
import {
  bestValue,
  formatMs,
  formatPercent,
  formatScore,
  isBest,
  parseEvalCases,
  parseEvalConfig,
  parseEvalRun,
  parseEvalRuns,
  parsePromoteDiff,
  rateTone,
  runStatusTone,
  runSummary,
} from "./evalContract.ts";
import type {
  EvalCaseList,
  EvalConfig,
  EvalModelScore,
  EvalPromoteDiff,
  EvalRun,
  ScoreColumn,
} from "./evalContract.ts";

const POLL_MS = 3000;

type LoadState =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; config: EvalConfig; runs: EvalRun[] };

type DrawerState = {
  runId: string;
  model: string | null;
  category: string | null;
} | null;

async function detailMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (isRecord(body) && typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    /* fall through */
  }
  return fallback;
}

function ScoreCell({
  score,
  scores,
  column,
  format,
}: {
  score: EvalModelScore;
  scores: EvalModelScore[];
  column: ScoreColumn;
  format: (value: number | null) => string;
}) {
  const value = score[column];
  const best = isBest(scores, column, value);
  return (
    <td className={best ? "eval-best" : undefined}>
      {format(value)}
      {best && <span className="eval-best-tag" aria-label="best in column">★</span>}
    </td>
  );
}

function CaseDrawer({
  drawer,
  onClose,
}: {
  drawer: DrawerState;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<"all" | "failed" | "passed">("failed");
  const [cases, setCases] = useState<EvalCaseList | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (drawer === null) {
      return;
    }
    const current = drawer;
    const controller = new AbortController();
    async function load() {
      const params = new URLSearchParams({ status });
      if (current.model) params.set("model", current.model);
      if (current.category) params.set("category", current.category);
      try {
        const response = await fetch(
          `/api/v1/evals/runs/${current.runId}/cases?${params.toString()}`,
          { cache: "no-store", signal: controller.signal },
        );
        if (!response.ok) {
          setError(await detailMessage(response, "Cases are unavailable."));
          return;
        }
        setError("");
        setCases(parseEvalCases(await response.json()));
      } catch {
        if (!controller.signal.aborted) {
          setError("Cases are unavailable. Check the local service.");
        }
      }
    }
    void load();
    return () => controller.abort();
  }, [drawer, status]);

  if (drawer === null) {
    return null;
  }

  return (
    <div className="eval-drawer" role="dialog" aria-label="Case inspector">
      <header className="eval-drawer__header">
        <div>
          <h3>Case inspector</h3>
          <p className="panel-note">
            {drawer.model ? `Model: ${drawer.model} · ` : ""}
            {drawer.category ? `Category: ${drawer.category}` : "All categories"}
          </p>
        </div>
        <div className="eval-drawer__actions">
          <div className="tab-bar" role="tablist" aria-label="Case filter">
            {(["failed", "passed", "all"] as const).map((value) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={status === value}
                className={`tab-button${status === value ? " tab-button--active" : ""}`}
                onClick={() => setStatus(value)}
              >
                {value === "failed" ? "Failed" : value === "passed" ? "Passed" : "All"}
              </button>
            ))}
          </div>
          <button type="button" className="text-button" onClick={onClose}>
            Close
          </button>
        </div>
      </header>

      {error && (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}

      {cases === null && !error && <p role="status">Loading cases...</p>}
      {cases !== null && cases.cases.length === 0 && (
        <p className="panel-note">No {status} cases for this filter.</p>
      )}

      {cases !== null && cases.cases.length > 0 && (
        <TableScroll label="Eval cases">
          <table className="judgment-table eval-cases">
            <thead>
              <tr>
                <th scope="col">Case</th>
                <th scope="col">Model</th>
                <th scope="col">Category</th>
                <th scope="col">Result</th>
                <th scope="col">Latency</th>
                <th scope="col">Confidence</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {cases.cases.map((item, index) => (
                <tr key={`${item.description}-${index}`}>
                  <th scope="row">{item.description || "—"}</th>
                  <td>{item.model}</td>
                  <td>{item.category}</td>
                  <td>
                    <span
                      className={`status-badge status-badge--${item.passed ? "ok" : "danger"}`}
                    >
                      {item.passed ? "PASS" : "FAIL"}
                    </span>
                  </td>
                  <td>{formatMs(item.latency_ms)}</td>
                  <td>{formatScore(item.confidence)}</td>
                  <td>
                    <details>
                      <summary>Output</summary>
                      <p className="eval-case-input">{item.input || "(no input)"}</p>
                      <pre>{item.raw_output}</pre>
                      {item.failure_reasons.length > 0 && (
                        <p className="eval-case-reason">
                          {item.failure_reasons.join(" · ")}
                        </p>
                      )}
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </div>
  );
}

function PromoteDialog({
  model,
  csrfToken,
  onClose,
}: {
  model: string;
  csrfToken: string;
  onClose: () => void;
}) {
  const [agentId, setAgentId] = useState("");
  const [agents, setAgents] = useState<string[]>([]);
  const [endpoint, setEndpoint] = useState("http://127.0.0.1:11434/v1");
  const [preview, setPreview] = useState<EvalPromoteDiff | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    async function loadAgents() {
      try {
        const response = await fetch("/api/v1/settings/config", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          return;
        }
        const body: unknown = await response.json();
        if (isRecord(body) && isRecord(body.agent_routing)) {
          setAgents(Object.keys(body.agent_routing).sort());
        }
      } catch {
        /* leave the select empty */
      }
    }
    void loadAgents();
    return () => controller.abort();
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!agentId) {
      setError("Choose an agent role.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/v1/evals/promote", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          model,
          agent_id: agentId,
          provider: "CUSTOM",
          endpoint: endpoint.trim() || null,
        }),
      });
      if (!response.ok) {
        setError(await detailMessage(response, "Promote failed."));
        return;
      }
      const diff = parsePromoteDiff(await response.json());
      if (preview === null) {
        // First pass renders the diff; the second confirms it.
        setPreview(diff);
        return;
      }
      setSaved(`${model} promoted to ${agentId}.`);
    } catch {
      setError("Promote failed. Check the local service.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="eval-modal" role="dialog" aria-modal="true" aria-label="Promote model">
      <form className="eval-modal__body" onSubmit={(event) => void submit(event)}>
        <header className="eval-drawer__header">
          <h3>Promote {model}</h3>
          <button type="button" className="text-button" onClick={onClose}>
            Cancel
          </button>
        </header>
        <p className="panel-note">
          This writes the agent routing in SQLite and mirrors it to
          config/agent_settings.yaml. The mirror is documentation; SQLite is truth.
        </p>
        {error && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}
        {saved && (
          <p className="config-feedback" role="status">
            {saved}
          </p>
        )}
        <div className="models-grid models-grid--compact">
          <label htmlFor="promote-agent">
            Target agent role
            <select
              id="promote-agent"
              value={agentId}
              onChange={(event) => setAgentId(event.target.value)}
              required
            >
              <option value="">Select a role…</option>
              {agents.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor="promote-endpoint">
            Local endpoint (keyless)
            <input
              id="promote-endpoint"
              type="text"
              value={endpoint}
              onChange={(event) => setEndpoint(event.target.value)}
            />
          </label>
        </div>
        {preview !== null && (
          <div className="eval-diff">
            <div>
              <h4>Before</h4>
              <pre>{preview.yaml_before}</pre>
            </div>
            <div>
              <h4>After</h4>
              <pre>{preview.yaml_after}</pre>
            </div>
          </div>
        )}
        <button type="submit" className="auth-button" disabled={busy || saved !== ""}>
          {busy ? "Promoting..." : preview === null ? "Preview change" : "Confirm promote"}
        </button>
      </form>
    </div>
  );
}

export default function ModelEvalsPanel({ csrfToken }: { csrfToken: string }) {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [noCache, setNoCache] = useState(true);
  const [selectedRun, setSelectedRun] = useState<string>("");
  const [drawer, setDrawer] = useState<DrawerState>(null);
  const [promoteModel, setPromoteModel] = useState<string | null>(null);
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);

  const activeRun = useMemo(() => {
    if (load.state !== "ready") {
      return null;
    }
    return load.runs.find((run) => run.status === "running" || run.status === "queued") ?? null;
  }, [load]);

  const running = activeRun !== null;

  async function refresh(signal?: AbortSignal) {
    try {
      const [configResponse, runsResponse] = await Promise.all([
        fetch("/api/v1/evals/config", { cache: "no-store", signal }),
        fetch("/api/v1/evals/runs", { cache: "no-store", signal }),
      ]);
      if (!configResponse.ok || !runsResponse.ok) {
        throw new Error("Eval configuration unavailable");
      }
      const config = parseEvalConfig(await configResponse.json());
      const runs = parseEvalRuns(await runsResponse.json());
      setLoad({ state: "ready", config, runs });
      setSelectedRun((current) => {
        if (current && runs.some((run) => run.id === current)) {
          return current;
        }
        const preferred =
          runs.find((run) => run.status === "running") ??
          runs.find((run) => run.status === "completed") ??
          runs[0];
        return preferred ? preferred.id : "";
      });
    } catch (error) {
      if (signal?.aborted) {
        return;
      }
      setLoad({
        state: "error",
        message: error instanceof Error ? error.message : "Eval configuration unavailable",
      });
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!running) {
      return;
    }
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [running]);

  async function startRun() {
    setBusy(true);
    setActionError("");
    try {
      const response = await fetch("/api/v1/evals/runs", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          providers: selectedModels,
          categories: selectedCategories,
          no_cache: noCache,
        }),
      });
      if (!response.ok) {
        setActionError(await detailMessage(response, "Could not start the benchmark."));
        return;
      }
      const run = parseEvalRun(await response.json());
      setSelectedRun(run.id);
      await refresh();
    } catch {
      setActionError("Could not start the benchmark. Check the local service.");
    } finally {
      setBusy(false);
    }
  }

  async function cancelRun() {
    if (activeRun === null) {
      return;
    }
    setBusy(true);
    setActionError("");
    try {
      const response = await fetch(`/api/v1/evals/runs/${activeRun.id}/cancel`, {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (!response.ok) {
        setActionError(await detailMessage(response, "Could not cancel the run."));
        return;
      }
      await refresh();
    } catch {
      setActionError("Could not cancel the run. Check the local service.");
    } finally {
      setBusy(false);
    }
  }

  if (load.state === "loading") {
    return (
      <section className="settings-panel" aria-label="Model evals">
        <p role="status">Loading eval configuration...</p>
      </section>
    );
  }
  if (load.state === "error") {
    return (
      <section className="settings-panel" aria-label="Model evals">
        <p role="alert">{load.message}</p>
      </section>
    );
  }

  const { config, runs } = load;
  const run = runs.find((entry) => entry.id === selectedRun) ?? null;
  const scores = run?.scores ?? [];
  const categoryColumns =
    run !== null && run.categories.length > 0 ? run.categories : config.categories;
  const selectedModelsValid = selectedModels.length > 0;

  return (
    <section className="settings-panel eval-panel" aria-labelledby="evals-title">
      <div className="panel-header">
        <div>
          <h2 id="evals-title">Model evals</h2>
          <p className="panel-note">
            Benchmark local Apple Silicon LLMs against the 50-case trading golden
            set. Promptfoo runs one model at a time so only one stays resident in
            unified memory.
          </p>
        </div>
        <span className="status-badge status-badge--provisional">SEQUENTIAL</span>
      </div>

      <div className="eval-launcher">
        <div className="eval-launcher__group">
          <h3>Models</h3>
          {config.providers.map((provider) => (
            <label key={provider.id} className="eval-check">
              <input
                type="checkbox"
                checked={selectedModels.includes(provider.id)}
                disabled={running || busy}
                onChange={(event) =>
                  setSelectedModels((current) =>
                    event.target.checked
                      ? [...current, provider.id]
                      : current.filter((item) => item !== provider.id),
                  )
                }
              />
              {provider.label}
            </label>
          ))}
          <p className="eval-hint">
            Leave all unchecked to run every configured model.
          </p>
        </div>

        <div className="eval-launcher__group">
          <h3>Categories</h3>
          {config.categories.map((category) => (
            <label key={category} className="eval-check">
              <input
                type="checkbox"
                checked={selectedCategories.includes(category)}
                disabled={running || busy}
                onChange={(event) =>
                  setSelectedCategories((current) =>
                    event.target.checked
                      ? [...current, category]
                      : current.filter((item) => item !== category),
                  )
                }
              />
              {category}
            </label>
          ))}
          <p className="eval-hint">Optional; all categories run when none selected.</p>
        </div>

        <div className="eval-launcher__group eval-launcher__controls">
          <label className="eval-check">
            <input
              type="checkbox"
              checked={noCache}
              disabled={running || busy}
              onChange={(event) => setNoCache(event.target.checked)}
            />
            No cache
          </label>
          <p className="tag-note">max-concurrency: 1 · safe for 24&nbsp;GB RAM</p>
          <div className="eval-actions">
            <button
              type="button"
              className="auth-button"
              onClick={() => void startRun()}
              disabled={running || busy}
            >
              {busy ? "Starting..." : "Run benchmark"}
            </button>
            <button
              type="button"
              className="text-button"
              onClick={() => void cancelRun()}
              disabled={!running || busy}
            >
              Cancel run
            </button>
          </div>
        </div>
      </div>

      {!config.tools.npx && (
        <p className="auth-error" role="alert">
          `npx` is not on PATH. Promptfoo cannot start until Node is installed.
        </p>
      )}
      {!config.tools.ollama && (
        <p className="panel-note">
          `ollama` is not on PATH. The Ollama providers will fail until it is running.
        </p>
      )}
      {actionError && (
        <p className="auth-error" role="alert">
          {actionError}
        </p>
      )}
      {activeRun !== null && (
        <p className="config-feedback" role="status">
          Run {activeRun.id.slice(0, 8)} is {activeRun.status}. Polling live progress.
        </p>
      )}

      <div className="eval-runbar">
        <label htmlFor="eval-run-select">
          Run
          <select
            id="eval-run-select"
            value={selectedRun}
            onChange={(event) => {
              setSelectedRun(event.target.value);
              setDrawer(null);
            }}
          >
            {runs.length === 0 && <option value="">No runs yet</option>}
            {runs.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {runSummary(entry)}
              </option>
            ))}
          </select>
        </label>
        {run !== null && (
          <span className={`status-badge status-badge--${runStatusTone(run.status)}`}>
            {run.status.toUpperCase()}
          </span>
        )}
        {run !== null && run.results_json_path !== null && (
          <a
            className="text-button"
            href={`/api/v1/evals/runs/${run.id}/scorecard.csv`}
            download
          >
            Download scorecard.csv
          </a>
        )}
      </div>

      {run !== null && run.error_message !== null && (
        <p className="auth-error" role="alert">
          {run.error_message}
        </p>
      )}

      {run !== null && scores.length === 0 && (
        <p className="panel-note">
          {run.status === "completed"
            ? "This run produced no scorecard rows."
            : "Scores appear here when the run completes."}
        </p>
      )}

      {scores.length > 0 && (
        <>
          <h3>Scorecard</h3>
          <TableScroll label="Model scorecard">
            <table className="judgment-table eval-scorecard">
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  <th scope="col">Overall</th>
                  <th scope="col">Task acc.</th>
                  <th scope="col">JSON parse</th>
                  <th scope="col">p50</th>
                  <th scope="col">p95</th>
                  <th scope="col">Brier ↓</th>
                  <th scope="col">ECE-5 ↓</th>
                  <th scope="col">Calib N</th>
                  <th scope="col">Cases</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {scores.map((score) => (
                  <tr key={score.model}>
                    <th scope="row">{score.model}</th>
                    <ScoreCell
                      score={score}
                      scores={scores}
                      column="overall_pass_rate"
                      format={formatPercent}
                    />
                    <ScoreCell
                      score={score}
                      scores={scores}
                      column="accuracy_pass_rate"
                      format={formatPercent}
                    />
                    <ScoreCell
                      score={score}
                      scores={scores}
                      column="json_parse_rate"
                      format={formatPercent}
                    />
                    <ScoreCell
                      score={score}
                      scores={scores}
                      column="p50_ms"
                      format={formatMs}
                    />
                    <ScoreCell
                      score={score}
                      scores={scores}
                      column="p95_ms"
                      format={formatMs}
                    />
                    <ScoreCell
                      score={score}
                      scores={scores}
                      column="brier_score"
                      format={(value) => formatScore(value)}
                    />
                    <ScoreCell
                      score={score}
                      scores={scores}
                      column="ece_5bin"
                      format={(value) => formatScore(value)}
                    />
                    <td>{score.brier_n}</td>
                    <td>{score.total_cases}</td>
                    <td>
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => setPromoteModel(score.model)}
                      >
                        Promote
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>

          <h3>Pass rate by category</h3>
          <TableScroll label="Category pass rate matrix">
            <table className="judgment-table eval-matrix">
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  {categoryColumns.map((category) => (
                    <th scope="col" key={category}>
                      {category}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {scores.map((score) => (
                  <tr key={score.model}>
                    <th scope="row">{score.model}</th>
                    {categoryColumns.map((category) => {
                      const rate = score.category_rates[category] ?? null;
                      return (
                        <td key={category} className={`eval-cell eval-cell--${rateTone(rate)}`}>
                          <button
                            type="button"
                            className="eval-cell__button"
                            onClick={() => {
                              if (run !== null) {
                                setDrawer({
                                  runId: run.id,
                                  model: score.model,
                                  category,
                                });
                              }
                            }}
                          >
                            {formatPercent(rate)}
                          </button>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>

          <div className="eval-actions">
            <button
              type="button"
              className="text-button"
              onClick={() => {
                if (run !== null) {
                  setDrawer({ runId: run.id, model: null, category: null });
                }
              }}
            >
              Inspect cases
            </button>
            <span className="eval-hint">
              Legend: green ≥ 85% · amber 65–84% · red &lt; 65%
            </span>
          </div>
        </>
      )}

      {bestValue(scores, "overall_pass_rate") !== null && (
        <p className="eval-footnote">
          Brier and ECE-5 are calibration errors: lower is better.★ marks the best
          value in each column.
        </p>
      )}

      {selectedModelsValid && running && (
        <p className="eval-hint">A run is active; the launcher is disabled until it ends.</p>
      )}

      {drawer !== null && (
        <CaseDrawer drawer={drawer} onClose={() => setDrawer(null)} />
      )}
      {promoteModel !== null && (
        <PromoteDialog
          model={promoteModel}
          csrfToken={csrfToken}
          onClose={() => setPromoteModel(null)}
        />
      )}
    </section>
  );
}
