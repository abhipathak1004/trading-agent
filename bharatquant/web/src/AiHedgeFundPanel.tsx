import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";

import EquityCurve from "./EquityCurve";
import {
  PERSONAS,
  PERSONA_LABELS,
  QUANT_MODELS,
  chainTone,
  clampLabel,
  formatMoney,
  formatNumber,
  formatPct,
  parseAiHfStatus,
  parseBacktestDetail,
  parseBacktestList,
  parseFund,
  parseFundList,
  parseLedger,
  parseMandate,
  parseMandateList,
  parsePreview,
  parseRunProgress,
  phaseTone,
  statusTone,
} from "./hedgeFundContract";
import type {
  AiHfStatus,
  BacktestDetail,
  BacktestSummary,
  Fund,
  Ledger,
  Mandate,
  Preview,
  Progress,
} from "./hedgeFundContract";

type SubView = "mandates" | "backtester" | "paper";

const SUB_VIEWS: [SubView, string][] = [
  ["mandates", "Mandates"],
  ["backtester", "Backtester"],
  ["paper", "Paper desk"],
];

const POLL_MS = 2000;

type Load<T> =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; value: T };

async function detail(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null && "detail" in body) {
      const value = (body as { detail: unknown }).detail;
      if (typeof value === "string") {
        return value;
      }
    }
  } catch {
    /* fall through to the fallback */
  }
  return fallback;
}

async function write(path: string, method: string, csrfToken: string, body?: unknown) {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await detail(response, `${method} ${path} failed (${response.status})`));
  }
  return response;
}

export default function AiHedgeFundPanel({
  csrfToken,
  onOpenSettings,
}: {
  csrfToken: string;
  onOpenSettings: () => void;
}) {
  const [view, setView] = useState<SubView>("mandates");
  const [status, setStatus] = useState<Load<AiHfStatus>>({ state: "loading" });

  const refreshStatus = useCallback(async (signal?: AbortSignal) => {
    try {
      const response = await fetch("/api/v1/aihf/status", { cache: "no-store", signal });
      if (!response.ok) {
        throw new Error(await detail(response, "AI Hedge Fund status unavailable"));
      }
      setStatus({ state: "ready", value: parseAiHfStatus(await response.json()) });
    } catch (error) {
      if (signal?.aborted) {
        return;
      }
      setStatus({ state: "error", message: error instanceof Error ? error.message : "Unavailable" });
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refreshStatus(controller.signal);
    return () => controller.abort();
  }, [refreshStatus]);

  const ready = status.state === "ready" ? status.value : null;

  return (
    <div className="ahf">
      <header className="ahf-header" aria-label="AI Hedge Fund configuration">
        <div className="ahf-header__identity">
          <p className="ahf-header__eyebrow">AI HEDGE FUND · IN-PROCESS ENGINE</p>
          <p className="ahf-header__runtime">
            {status.state === "loading" && "Reading the engine…"}
            {status.state === "error" && status.message}
            {ready && (
              <>
                <strong>{ready.active_provider ?? "no provider bridged"}</strong>
                {ready.active_model ? ` · ${ready.active_model}` : " · model per run"}
              </>
            )}
          </p>
        </div>

        <div className="ahf-header__controls">
          {ready && (
            <div className="ahf-header__badges">
              <span className="status-badge status-badge--ok">
                engine {ready.version ?? "in-process"}
              </span>
              <span
                className={`status-badge status-badge--${ready.has_llm_key ? "ok" : "warn"}`}
              >
                {ready.has_llm_key ? "model key" : "no model key"}
              </span>
              <span className="status-badge status-badge--ok">
                data: this app
              </span>
            </div>
          )}
        </div>
      </header>

      {ready && !ready.has_llm_key && (
        <div className="mode-banner mode-banner--warn" role="alert">
          <span aria-hidden="true">!</span>
          <div>
            <strong>No model provider is configured</strong>
            <p>
              The investor personas need a model. Configure a provider and key in
              Settings → Models &amp; keys; each persona routes by its own agent
              entry, so you can also point one persona at a different tier.
            </p>
            <button type="button" className="auth-button auth-button--compact" onClick={onOpenSettings}>
              Open Settings
            </button>
          </div>
        </div>
      )}

      <div className="tab-bar" role="tablist" aria-label="AI Hedge Fund sections">
        {SUB_VIEWS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={view === key}
            className={`tab-button${view === key ? " tab-button--active" : ""}`}
            onClick={() => setView(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {view === "mandates" && (
        <MandatesView csrfToken={csrfToken} canRun={ready?.can_run ?? false} />
      )}
      {view === "backtester" && (
        <BacktesterView csrfToken={csrfToken} canRun={ready?.can_run ?? false} />
      )}
      {view === "paper" && (
        <PaperView csrfToken={csrfToken} canRun={ready?.can_run ?? false} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Mandates
// ---------------------------------------------------------------------------

function MandatesView({ csrfToken, canRun }: { csrfToken: string; canRun: boolean }) {
  const [list, setList] = useState<Load<Mandate[]>>({ state: "loading" });
  const [template, setTemplate] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<"form" | "yaml">("yaml");
  const [message, setMessage] = useState<{ tone: "ok" | "danger"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch("/api/v1/aihf/mandates", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(await detail(response, "Mandates unavailable"));
        }
        const parsed = parseMandateList(await response.json());
        setList({ state: "ready", value: parsed.mandates });
        setTemplate(parsed.template);
        if (selected === null && parsed.mandates.length > 0) {
          select(parsed.mandates[0]);
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setList({
            state: "error",
            message: error instanceof Error ? error.message : "Unavailable",
          });
        }
      }
    }
    function select(mandate: Mandate) {
      setSelected(mandate.name);
      setDraft(mandate.yaml);
    }
    void load();
    return () => controller.abort();
    // `selected` is intentionally read for the first-paint default only.
  }, [retry]);

  const current = useMemo(
    () => (list.state === "ready" ? (list.value.find((m) => m.name === selected) ?? null) : null),
    [list, selected],
  );

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const creating = selected === null || !current;
      const path = creating ? "/api/v1/aihf/mandates" : `/api/v1/aihf/mandates/${selected}`;
      const response = await write(path, creating ? "POST" : "PUT", csrfToken, { yaml: draft });
      const saved = parseMandate(await response.json());
      setSelected(saved.name);
      setDraft(saved.yaml);
      setMessage({ tone: "ok", text: `Saved ${saved.name}.` });
      setRetry((value) => value + 1);
    } catch (error) {
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Save failed" });
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!selected) {
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      await write(`/api/v1/aihf/mandates/${selected}`, "DELETE", csrfToken);
      setSelected(null);
      setDraft("");
      setMessage({ tone: "ok", text: "Mandate deleted." });
      setRetry((value) => value + 1);
    } catch (error) {
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Delete failed" });
    } finally {
      setBusy(false);
    }
  }

  function startNew() {
    setSelected(null);
    setDraft(template);
    setMode("yaml");
    setMessage(null);
  }

  return (
    <div className="ahf-split">
      <section className="settings-panel ahf-split__rail" aria-labelledby="ahf-mandates-title">
        <div className="panel-header">
          <h2 id="ahf-mandates-title">Mandates</h2>
          <button type="button" className="auth-button auth-button--compact" onClick={startNew}>
            New mandate
          </button>
        </div>
        <p className="panel-note">
          A mandate is aihf&apos;s desk: strategies, staff, risk and cadence. It names no
          tickers — those are chosen per run.
        </p>
        {list.state === "loading" && <p role="status">Loading…</p>}
        {list.state === "error" && (
          <p className="auth-error" role="alert">
            {list.message}{" "}
            <button type="button" className="text-button" onClick={() => setRetry((v) => v + 1)}>
              Retry
            </button>
          </p>
        )}
        {list.state === "ready" && list.value.length === 0 && (
          <p className="panel-note">No mandates saved yet.</p>
        )}
        {list.state === "ready" && list.value.length > 0 && (
          <ul className="ahf-list">
            {list.value.map((mandate) => (
              <li key={mandate.name}>
                <button
                  type="button"
                  className={`ahf-list__item${mandate.name === selected ? " ahf-list__item--active" : ""}`}
                  onClick={() => {
                    setSelected(mandate.name);
                    setDraft(mandate.yaml);
                    setMessage(null);
                  }}
                >
                  <span className="ahf-list__title">{mandate.name}</span>
                  <span className="ahf-list__meta">
                    {mandate.valid ? (
                      <>
                        {(mandate.summary?.personas ?? []).map((p) => PERSONA_LABELS[p] ?? p).join(" · ") ||
                          "no personas"}
                        {mandate.summary?.quant_models.length ? " · PEAD" : ""}
                      </>
                    ) : (
                      <span className="ahf-list__invalid">{mandate.error ?? "invalid"}</span>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="settings-panel ahf-split__main" aria-labelledby="ahf-editor-title">
        <div className="panel-header">
          <h2 id="ahf-editor-title">{selected ? `Edit ${selected}` : "New mandate"}</h2>
          <div className="ahf-toggle" role="group" aria-label="Editor mode">
            <button
              type="button"
              className={`text-button${mode === "form" ? " text-button--active" : ""}`}
              onClick={() => setMode("form")}
            >
              Form
            </button>
            <button
              type="button"
              className={`text-button${mode === "yaml" ? " text-button--active" : ""}`}
              onClick={() => setMode("yaml")}
            >
              YAML
            </button>
          </div>
        </div>

        {current?.summary && mode === "form" ? (
          <MandateForm summary={current.summary} />
        ) : (
          <p className="panel-note">
            The YAML is the file aihf reads. It is validated by aihf&apos;s own{" "}
            <code>FundSpec</code> before it is written, so an invalid mandate never lands.
          </p>
        )}

        {current && current.summary && mode === "form" && (
          <details className="ahf-details">
            <summary>Deterministic risk limits (read-only here)</summary>
            <dl className="settings-facts">
              <div>
                <dt>Max single-stock weight</dt>
                <dd>{formatPct(current.summary.risk.max_position_pct, 0)}</dd>
              </div>
              <div>
                <dt>Leverage cap (gross)</dt>
                <dd>{formatNumber(current.summary.risk.max_gross_exposure, 2)}×</dd>
              </div>
              <div>
                <dt>Drawdown half-size</dt>
                <dd>{formatPct(current.summary.risk.halve_drawdown_pct, 0)}</dd>
              </div>
              <div>
                <dt>Drawdown cash-out</dt>
                <dd>{formatPct(current.summary.risk.max_drawdown_pct, 0)}</dd>
              </div>
            </dl>
            <p className="panel-note">
              These are hard limits. aihf clamps the book in deterministic code; the LLM never
              sets exposure, and a clamp only ever shrinks a position.
            </p>
          </details>
        )}

        <form className="config-form" onSubmit={(event) => void save(event)}>
          <label htmlFor="ahf-mandate-yaml">
            Mandate YAML
            <textarea
              id="ahf-mandate-yaml"
              className="ahf-yaml"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              spellCheck={false}
              rows={18}
            />
          </label>
          {!canRun && (
            <p className="panel-note">
              Saving works now. Running needs the data key and a model key from Settings.
            </p>
          )}
          <div className="ahf-actions">
            <button type="submit" className="auth-button" disabled={busy || draft.trim().length === 0}>
              {selected ? "Save mandate" : "Create mandate"}
            </button>
            {selected && (
              <button type="button" className="text-button" onClick={() => void remove()} disabled={busy}>
                Delete
              </button>
            )}
          </div>
          {message && (
            <p
              className={message.tone === "ok" ? "config-feedback" : "auth-error"}
              role={message.tone === "ok" ? "status" : "alert"}
            >
              {message.text}
            </p>
          )}
        </form>
      </section>
    </div>
  );
}

/**
 * A read-only projection of a mandate's strategies and risk.
 *
 * The YAML above is the source of truth the owner edits; the checkboxes below
 * state what that YAML currently selects so the shapes aihf supports (five
 * investor personas plus one quant model) are visible rather than guessed at.
 */
function MandateForm({ summary }: { summary: NonNullable<Mandate["summary"]> }) {
  const selected = new Set(summary.strategies.flatMap((strategy) => strategy.models));
  return (
    <div className="ahf-form-summary">
      <fieldset className="ahf-fieldset">
        <legend>Personas &amp; strategies</legend>
        {[...PERSONAS, ...QUANT_MODELS].map((name) => (
          <label key={name} className="eval-check">
            <input type="checkbox" checked={selected.has(name)} disabled readOnly />
            {PERSONA_LABELS[name] ?? name}
          </label>
        ))}
      </fieldset>
      <dl className="settings-facts">
        <div>
          <dt>Capital</dt>
          <dd>{formatMoney(summary.capital)}</dd>
        </div>
        <div>
          <dt>Cadence</dt>
          <dd>{summary.rebalance}</dd>
        </div>
        <div>
          <dt>Benchmark</dt>
          <dd>{summary.benchmark}</dd>
        </div>
        <div>
          <dt>Universe rule</dt>
          <dd>{summary.has_universe_rule ? "picks its own names" : "tickers per run"}</dd>
        </div>
      </dl>
      <ul className="ahf-pods">
        {summary.strategies.map((strategy) => (
          <li key={strategy.name}>
            <strong>{strategy.title}</strong>
            <span>
              {strategy.mode.replace("_", " ")} · weight {formatNumber(strategy.weight, 2)} ·{" "}
              {strategy.models.join(", ")}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Backtester
// ---------------------------------------------------------------------------

function BacktesterView({ csrfToken, canRun }: { csrfToken: string; canRun: boolean }) {
  const [mandates, setMandates] = useState<Load<Mandate[]>>({ state: "loading" });
  const [runs, setRuns] = useState<Load<BacktestSummary[]>>({ state: "loading" });
  const [mandate, setMandate] = useState("");
  const [tickers, setTickers] = useState("RELIANCE, TCS, INFY");
  const [start, setStart] = useState(defaultStart());
  const [end, setEnd] = useState(defaultEnd());
  const [runId, setRunId] = useState<string | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [detailState, setDetailState] = useState<Load<BacktestDetail> | null>(null);
  const [openSession, setOpenSession] = useState<string | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "danger"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshRuns = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/aihf/backtests", { cache: "no-store" });
      if (!response.ok) {
        throw new Error(await detail(response, "Saved backtests unavailable"));
      }
      setRuns({ state: "ready", value: parseBacktestList(await response.json()) });
    } catch (error) {
      setRuns({
        state: "error",
        message: error instanceof Error ? error.message : "Unavailable",
      });
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch("/api/v1/aihf/mandates", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(await detail(response, "Mandates unavailable"));
        }
        const parsed = parseMandateList(await response.json());
        setMandates({ state: "ready", value: parsed.mandates });
        const first = parsed.mandates.find((entry) => entry.valid);
        if (first) {
          setMandate(first.name);
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setMandates({
            state: "error",
            message: error instanceof Error ? error.message : "Unavailable",
          });
        }
      }
    }
    void load();
    void refreshRuns();
    return () => controller.abort();
  }, [refreshRuns]);

  // Poll the running backtest's journal; the SSE stream is the same data.
  useEffect(() => {
    if (!runId) {
      return;
    }
    let stop = false;
    async function poll() {
      try {
        const response = await fetch(`/api/v1/aihf/backtest/${runId}`, { cache: "no-store" });
        if (!response.ok) {
          return;
        }
        const parsed = parseRunProgress(await response.json());
        setProgress(parsed.progress);
        if (parsed.progress?.done && !stop) {
          stop = true;
          void refreshRuns();
        }
      } catch {
        /* a dropped poll is retried on the next tick */
      }
    }
    const timer = window.setInterval(() => {
      void poll();
      if (stop) {
        window.clearInterval(timer);
      }
    }, POLL_MS);
    void poll();
    return () => window.clearInterval(timer);
  }, [runId, refreshRuns]);

  async function startRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    setDetailState(null);
    setProgress(null);
    try {
      const response = await write("/api/v1/aihf/backtest", "POST", csrfToken, {
        mandate,
        tickers: tickers.split(",").map((value) => value.trim()).filter(Boolean),
        start_date: start,
        end_date: end,
      });
      const body = (await response.json()) as { run_id: string };
      setRunId(body.run_id);
      setMessage({ tone: "ok", text: `Queued ${body.run_id}.` });
    } catch (error) {
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Run failed" });
    } finally {
      setBusy(false);
    }
  }

  async function openResult(id: string) {
    setDetailState({ state: "loading" });
    try {
      const response = await fetch(`/api/v1/aihf/backtests/${id}`, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(await detail(response, "Result unavailable"));
      }
      setDetailState({ state: "ready", value: parseBacktestDetail(await response.json()) });
      setOpenSession(null);
    } catch (error) {
      setDetailState({
        state: "error",
        message: error instanceof Error ? error.message : "Unavailable",
      });
    }
  }

  const detailValue = detailState?.state === "ready" ? detailState.value : null;
  const sessionForDrawer =
    detailValue && openSession
      ? (detailValue.sessions.find((session) => session.session === openSession) ?? null)
      : null;

  return (
    <div className="ahf-stack">
      <section className="settings-panel" aria-labelledby="ahf-run-title">
        <h2 id="ahf-run-title">Run a backtest</h2>
        <p className="panel-note">
          The engine replays the mandate over history: a decision at one session&apos;s close is
          executed at the next one, so nothing can look ahead. Progress below counts real
          completed sessions; there is no invented percentage.
        </p>
        <form className="config-form" onSubmit={(event) => void startRun(event)}>
          <div className="models-grid models-grid--compact">
            <label htmlFor="ahf-mandate">
              Mandate
              <select
                id="ahf-mandate"
                value={mandate}
                onChange={(event) => setMandate(event.target.value)}
                required
              >
                <option value="">Select a mandate…</option>
                {(mandates.state === "ready" ? mandates.value : [])
                  .filter((entry) => entry.valid)
                  .map((entry) => (
                    <option key={entry.name} value={entry.name}>
                      {entry.name}
                    </option>
                  ))}
              </select>
            </label>
            <label htmlFor="ahf-start">
              Start
              <input id="ahf-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
            </label>
            <label htmlFor="ahf-end">
              End
              <input id="ahf-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </label>
          </div>
          <label htmlFor="ahf-tickers">
            Ticker universe (comma separated)
            <input
              id="ahf-tickers"
              type="text"
              value={tickers}
              onChange={(event) => setTickers(event.target.value)}
              required
            />
          </label>
          <div className="ahf-actions">
            <button
              type="submit"
              className="auth-button"
              disabled={busy || !canRun || mandate === ""}
            >
              Run backtest
            </button>
            {runId && progress && (
              <span className={`status-badge status-badge--${phaseTone(progress.phase)}`}>
                {progress.phase} · {Math.round(progress.elapsed_seconds)}s
              </span>
            )}
          </div>
          {!canRun && (
            <p className="panel-note">
              Set the Financial Datasets key and a model key in Settings before running.
            </p>
          )}
          {message && (
            <p
              className={message.tone === "ok" ? "config-feedback" : "auth-error"}
              role={message.tone === "ok" ? "status" : "alert"}
            >
              {message.text}
            </p>
          )}
        </form>

        {progress && (
          <div className="ahf-progress" role="group" aria-label="Backtest progress">
            <p className="ahf-progress__phase" role="status">
              {progress.detail || progress.phase}
              {progress.error ? ` — ${progress.error}` : ""}
            </p>
            {progress.lines.length > 0 && (
              <details className="ahf-details">
                <summary>Engine log ({progress.lines.length} lines)</summary>
                <pre className="ahf-log">{progress.lines.slice(-60).join("\n")}</pre>
              </details>
            )}
          </div>
        )}
      </section>

      <section className="settings-panel" aria-labelledby="ahf-saved-title">
        <div className="panel-header">
          <h2 id="ahf-saved-title">Saved results</h2>
          <button type="button" className="text-button" onClick={() => void refreshRuns()}>
            Refresh
          </button>
        </div>
        {runs.state === "loading" && <p role="status">Loading…</p>}
        {runs.state === "error" && (
          <p className="auth-error" role="alert">
            {runs.message}
          </p>
        )}
        {runs.state === "ready" && runs.value.length === 0 && (
          <p className="panel-note">No saved backtests yet.</p>
        )}
        {runs.state === "ready" && runs.value.length > 0 && (
          <div className="table-scroll" role="region" aria-label="Saved backtests" tabIndex={0}>
            <table className="judgment-table">
              <caption className="ahf-curve__caption-text">
                One row per saved run in aihf&apos;s research directory.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Run</th>
                  <th scope="col">Window</th>
                  <th scope="col">Return</th>
                  <th scope="col">Benchmark</th>
                  <th scope="col">Sharpe</th>
                  <th scope="col">Max DD</th>
                  <th scope="col">Cycles</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {runs.value.map((run) => (
                  <tr key={run.run_id}>
                    <th scope="row">{run.fund}</th>
                    <td>
                      {run.start} → {run.end}
                    </td>
                    <td>{formatPct(run.metrics.total_return_pct)}</td>
                    <td>{formatPct(run.metrics.benchmark_return_pct)}</td>
                    <td>{formatNumber(run.metrics.sharpe_ratio)}</td>
                    <td>{formatPct(run.metrics.max_drawdown_pct)}</td>
                    <td>{run.metrics.n_cycles}</td>
                    <td>
                      <button type="button" className="text-button" onClick={() => void openResult(run.run_id)}>
                        Open
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {detailState?.state === "loading" && <p role="status">Loading result…</p>}
      {detailState?.state === "error" && (
        <p className="auth-error" role="alert">
          {detailState.message}
        </p>
      )}

      {detailValue && (
        <>
          <section className="settings-panel" aria-labelledby="ahf-kpi-title">
            <h2 id="ahf-kpi-title">{detailValue.summary.fund}</h2>
            <p className="panel-note">
              {detailValue.summary.universe.join(", ")} · {detailValue.summary.start} →{" "}
              {detailValue.summary.end} · {detailValue.summary.rebalance} rebalance vs{" "}
              {detailValue.summary.benchmark}
            </p>
            <div className="overview-grid">
              <Kpi label="Total return" value={formatPct(detailValue.summary.metrics.total_return_pct)} />
              <Kpi
                label="Benchmark"
                value={formatPct(detailValue.summary.metrics.benchmark_return_pct)}
              />
              <Kpi label="Sharpe" value={formatNumber(detailValue.summary.metrics.sharpe_ratio)} />
              <Kpi
                label="Max drawdown"
                value={formatPct(detailValue.summary.metrics.max_drawdown_pct)}
              />
              <Kpi label="Cycles" value={String(detailValue.summary.metrics.n_cycles)} />
              <Kpi
                label="Trading cost"
                value={formatMoney(detailValue.summary.metrics.trading_cost)}
              />
            </div>
          </section>

          <section className="settings-panel" aria-labelledby="ahf-curve-title">
            <h2 id="ahf-curve-title">Equity curve</h2>
            <EquityCurve
              dates={detailValue.dates}
              series={[
                { label: "Strategy NAV", values: detailValue.nav, tone: "accent" },
                { label: `${detailValue.summary.benchmark} (scaled)`, values: detailValue.benchmark_nav, tone: "muted" },
              ]}
            />
          </section>

          <section className="settings-panel" aria-labelledby="ahf-trades-title">
            <h2 id="ahf-trades-title">Trade log</h2>
            <p className="panel-note">
              A signal at session T is executed at T+1. Click a row to read the reasoning
              behind that decision.
            </p>
            {detailValue.trades.length === 0 ? (
              <p className="panel-note">No executions in this window.</p>
            ) : (
              <div className="table-scroll" role="region" aria-label="Trade log" tabIndex={0}>
                <table className="judgment-table">
                  <thead>
                    <tr>
                      <th scope="col">Signal</th>
                      <th scope="col">Executed</th>
                      <th scope="col">Ticker</th>
                      <th scope="col">Side</th>
                      <th scope="col">Shares</th>
                      <th scope="col">Fill</th>
                      <th scope="col">Cost</th>
                      <th scope="col" />
                    </tr>
                  </thead>
                  <tbody>
                    {detailValue.trades.slice(0, 500).map((trade, index) => (
                      <tr key={`${trade.execution_date}-${trade.ticker}-${index}`}>
                        <th scope="row">{trade.signal_date}</th>
                        <td>{trade.execution_date}</td>
                        <td>{trade.ticker}</td>
                        <td>{trade.side}</td>
                        <td>{trade.quantity}</td>
                        <td>{formatMoney(trade.price)}</td>
                        <td>{formatMoney(trade.cost)}</td>
                        <td>
                          <button
                            type="button"
                            className="text-button"
                            onClick={() => setOpenSession(trade.signal_date)}
                          >
                            Reasoning
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}

      {sessionForDrawer && (
        <ReasoningDrawer session={sessionForDrawer} onClose={() => setOpenSession(null)} />
      )}
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="overview-card">
      <p className="overview-card__label">{label}</p>
      <p className="overview-card__value">{value}</p>
    </div>
  );
}

function defaultEnd(): string {
  const now = new Date();
  return now.toISOString().slice(0, 10);
}

function defaultStart(): string {
  const now = new Date();
  now.setMonth(now.getMonth() - 6);
  return now.toISOString().slice(0, 10);
}

function ReasoningDrawer({
  session,
  onClose,
}: {
  session: NonNullable<BacktestDetail["sessions"]>[number];
  onClose: () => void;
}) {
  const decision = session.decision;
  return (
    <div className="eval-drawer" role="dialog" aria-label={`Reasoning for ${session.session}`}>
      <header className="eval-drawer__header">
        <h3>Reasoning · {session.session}</h3>
        <button type="button" className="text-button" onClick={onClose}>
          Close
        </button>
      </header>
      <p className="panel-note">
        NAV {formatMoney(session.nav)} · {session.benchmark} at{" "}
        {formatMoney(session.benchmark_close)}
        {decision?.drawdown !== null && decision?.drawdown !== undefined
          ? ` · drawdown ${formatPct(decision.drawdown)}`
          : ""}
      </p>
      {decision === null ? (
        <p className="panel-note">This session made no decision.</p>
      ) : (
        <>
          {decision.clamps.length > 0 && (
            <div className="ahf-clamps">
              <h4>Risk gates that fired</h4>
              <ul>
                {decision.clamps.map((clamp, index) => (
                  <li key={`${clamp.limit}-${index}`}>
                    <strong>{clampLabel(clamp.limit)}</strong>
                    {clamp.ticker ? ` · ${clamp.ticker}` : ""} — {formatPct(clamp.before)} →{" "}
                    {formatPct(clamp.after)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {decision.strategies.map((strategy) => (
            <details key={strategy.name} className="ahf-details" open>
              <summary>
                {strategy.name} · slice {formatPct(strategy.slice, 0)}
                {strategy.flat_reason ? ` · flat: ${strategy.flat_reason}` : ""}
              </summary>
              {strategy.signals.length === 0 ? (
                <p className="panel-note">No views from this sleeve.</p>
              ) : (
                <ul className="ahf-signals">
                  {strategy.signals.map((signal, index) => (
                    <li key={`${signal.model_name}-${signal.ticker}-${index}`}>
                      <span className={`ahf-signals__side ahf-signals__side--${signal.side}`}>
                        {signal.side}
                      </span>
                      <strong>{signal.model_name}</strong> · {signal.ticker} ·{" "}
                      {signal.value.toFixed(2)}
                      {signal.reasoning && <p>{signal.reasoning}</p>}
                    </li>
                  ))}
                </ul>
              )}
            </details>
          ))}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Paper desk
// ---------------------------------------------------------------------------

function PaperView({ csrfToken, canRun }: { csrfToken: string; canRun: boolean }) {
  const [funds, setFunds] = useState<Load<Fund[]>>({ state: "loading" });
  const [mandates, setMandates] = useState<Mandate[]>([]);
  const [ledger, setLedger] = useState<Ledger | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewing, setPreviewing] = useState<string | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "danger"; text: string } | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const [fundsResponse, mandatesResponse] = await Promise.all([
          fetch("/api/v1/aihf/paper/funds", { cache: "no-store", signal: controller.signal }),
          fetch("/api/v1/aihf/mandates", { cache: "no-store", signal: controller.signal }),
        ]);
        if (!fundsResponse.ok) {
          throw new Error(await detail(fundsResponse, "Paper funds unavailable"));
        }
        setFunds({ state: "ready", value: parseFundList(await fundsResponse.json()) });
        if (mandatesResponse.ok) {
          setMandates(parseMandateList(await mandatesResponse.json()).mandates);
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setFunds({
            state: "error",
            message: error instanceof Error ? error.message : "Unavailable",
          });
        }
      }
    }
    void load();
    return () => controller.abort();
  }, [retry]);

  async function refreshLedger(name: string) {
    try {
      const response = await fetch(`/api/v1/aihf/paper/${name}/ledger`, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(await detail(response, "Ledger unavailable"));
      }
      setLedger(parseLedger(await response.json()));
    } catch (error) {
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Ledger failed" });
    }
  }

  async function toggleSwitch(fund: Fund) {
    setMessage(null);
    try {
      const action = fund.status === "ACTIVE" ? "halt" : "resume";
      const response = await write(
        `/api/v1/aihf/paper/${fund.name}/kill-switch`,
        "POST",
        csrfToken,
        { action, reason: action === "halt" ? "halted from the AI Hedge Fund tab" : undefined },
      );
      parseFund(await response.json());
      setMessage({
        tone: "ok",
        text: action === "halt" ? `${fund.name} halted.` : `${fund.name} resumed.`,
      });
      setRetry((value) => value + 1);
    } catch (error) {
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Failed" });
    }
  }

  async function runPreview(fund: Fund) {
    setPreviewing(fund.name);
    setMessage(null);
    try {
      const response = await write(
        `/api/v1/aihf/paper/${fund.name}/preview`,
        "POST",
        csrfToken,
        {},
      );
      setPreview(parsePreview(await response.json()));
    } catch (error) {
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Preview failed" });
    } finally {
      setPreviewing(null);
    }
  }

  async function approve() {
    if (!preview) {
      return;
    }
    try {
      const response = await write(
        `/api/v1/aihf/paper/${preview.fund}/execute`,
        "POST",
        csrfToken,
        { preview_id: preview.preview_id },
      );
      await response.json();
      setMessage({ tone: "ok", text: `Committed ${preview.session}.` });
      setPreview(null);
      setRetry((value) => value + 1);
      void refreshLedger(preview.fund);
    } catch (error) {
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Commit failed" });
    }
  }

  return (
    <div className="ahf-stack">
      <section className="settings-panel" aria-labelledby="ahf-funds-title">
        <div className="panel-header">
          <h2 id="ahf-funds-title">Paper funds</h2>
          <button
            type="button"
            className="auth-button auth-button--compact"
            onClick={() => setShowCreate(true)}
          >
            New paper fund
          </button>
        </div>
        <p className="panel-note">
          Each fund keeps a hash-chained ledger of real market sessions and fake money. A
          session is computed without touching the fund, then appended to the ledger only once
          you approve it — and the appended record's hash must match what the preview computed.
        </p>
        {funds.state === "loading" && <p role="status">Loading…</p>}
        {funds.state === "error" && (
          <p className="auth-error" role="alert">
            {funds.message}{" "}
            <button type="button" className="text-button" onClick={() => setRetry((v) => v + 1)}>
              Retry
            </button>
          </p>
        )}
        {funds.state === "ready" && funds.value.length === 0 && (
          <p className="panel-note">No paper funds yet.</p>
        )}
        {funds.state === "ready" && funds.value.length > 0 && (
          <div className="table-scroll" role="region" aria-label="Paper funds" tabIndex={0}>
            <table className="judgment-table">
              <thead>
                <tr>
                  <th scope="col">Fund</th>
                  <th scope="col">Mandate</th>
                  <th scope="col">Universe</th>
                  <th scope="col">NAV</th>
                  <th scope="col">Drawdown</th>
                  <th scope="col">Status</th>
                  <th scope="col">Chain</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {funds.value.map((fund) => (
                  <tr key={fund.name}>
                    <th scope="row">{fund.name}</th>
                    <td>{fund.mandate ?? "—"}</td>
                    <td>{fund.universe.join(", ") || "—"}</td>
                    <td>{formatMoney(fund.nav)}</td>
                    <td>{formatPct(fund.drawdown_pct)}</td>
                    <td>
                      <span className={`status-badge status-badge--${statusTone(fund.status)}`}>
                        {fund.status}
                      </span>
                      {fund.halt_reason && <p className="ahf-halt-reason">{fund.halt_reason}</p>}
                    </td>
                    <td>
                      <span className={`status-badge status-badge--${chainTone(fund.chain_verified, fund.problem)}`}>
                        {fund.problem ? "unreadable" : fund.chain_verified ? "verified" : "unverified"}
                      </span>
                    </td>
                    <td>
                      <div className="ahf-row-actions">
                        <button
                          type="button"
                          className="text-button"
                          disabled={fund.status === "HALTED" || previewing !== null}
                          onClick={() => void runPreview(fund)}
                        >
                          {previewing === fund.name ? "Computing…" : "Run next session"}
                        </button>
                        <button type="button" className="text-button" onClick={() => void refreshLedger(fund.name)}>
                          Ledger
                        </button>
                        <button
                          type="button"
                          className={`text-button${fund.status === "ACTIVE" ? " text-button--danger" : ""}`}
                          onClick={() => void toggleSwitch(fund)}
                        >
                          {fund.status === "ACTIVE" ? "Halt" : "Resume"}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {message && (
          <p
            className={message.tone === "ok" ? "config-feedback" : "auth-error"}
            role={message.tone === "ok" ? "status" : "alert"}
          >
            {message.text}
          </p>
        )}
        {!canRun && (
          <p className="panel-note">
            Running a session needs the data key and a model key; the kill switch works without
            either.
          </p>
        )}
      </section>

      {ledger && <LedgerView ledger={ledger} onClose={() => setLedger(null)} />}

      {showCreate && (
        <CreateFundModal
          csrfToken={csrfToken}
          mandates={mandates.filter((entry) => entry.valid)}
          onClose={() => setShowCreate(false)}
          onCreated={(name) => {
            setShowCreate(false);
            setMessage({ tone: "ok", text: `${name} deployed.` });
            setRetry((value) => value + 1);
          }}
          onError={(text) => setMessage({ tone: "danger", text })}
        />
      )}

      {preview && (
        <ApprovalModal
          preview={preview}
          onReject={() => {
            setPreview(null);
            setMessage({ tone: "ok", text: "Session rejected; the fund is unchanged." });
            setRetry((value) => value + 1);
          }}
          onApprove={() => void approve()}
        />
      )}

    </div>
  );
}

function LedgerView({ ledger, onClose }: { ledger: Ledger; onClose: () => void }) {
  return (
    <section className="settings-panel" aria-labelledby="ahf-ledger-title">
      <div className="panel-header">
        <h2 id="ahf-ledger-title">Ledger · {ledger.fund}</h2>
        <button type="button" className="text-button" onClick={onClose}>
          Close
        </button>
      </div>
      <p className="panel-note">
        <span className={`status-badge status-badge--${chainTone(ledger.chain_verified, ledger.problem)}`}>
          {ledger.chain_verified ? "Chain verified" : "Chain not verified"}
        </span>{" "}
        {ledger.problem ?? ledger.verify_error ?? `${ledger.session_total} sessions`}
      </p>
      {ledger.sessions.length === 0 ? (
        <p className="panel-note">No sessions recorded yet.</p>
      ) : (
        <div className="table-scroll" role="region" aria-label="Ledger sessions" tabIndex={0}>
          <table className="judgment-table">
            <thead>
              <tr>
                <th scope="col">Session</th>
                <th scope="col">NAV</th>
                <th scope="col">Cash</th>
                <th scope="col">Orders</th>
                <th scope="col">Entry hash</th>
              </tr>
            </thead>
            <tbody>
              {ledger.sessions.map((session) => (
                <tr key={session.session}>
                  <th scope="row">{session.session}</th>
                  <td>{formatMoney(session.nav)}</td>
                  <td>{formatMoney(session.cash)}</td>
                  <td>{session.executed?.orders.length ?? 0}</td>
                  <td className="ahf-hash">{session.hash.slice(0, 16)}…</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {ledger.events.length > 0 && (
        <details className="ahf-details">
          <summary>Event log ({ledger.events.length})</summary>
          <pre className="ahf-log">
            {ledger.events.map((event) => JSON.stringify(event)).join("\n")}
          </pre>
        </details>
      )}
    </section>
  );
}

function CreateFundModal({
  csrfToken,
  mandates,
  onClose,
  onCreated,
  onError,
}: {
  csrfToken: string;
  mandates: Mandate[];
  onClose: () => void;
  onCreated: (name: string) => void;
  onError: (text: string) => void;
}) {
  const [name, setName] = useState("");
  const [mandate, setMandate] = useState(mandates[0]?.name ?? "");
  const [tickers, setTickers] = useState("RELIANCE, TCS");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    try {
      const response = await write("/api/v1/aihf/paper/funds", "POST", csrfToken, {
        name,
        mandate,
        tickers: tickers.split(",").map((value) => value.trim()).filter(Boolean),
      });
      const fund = parseFund(await response.json());
      onCreated(fund.name);
    } catch (error) {
      onError(error instanceof Error ? error.message : "Create failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="eval-modal" role="dialog" aria-modal="true" aria-label="Create a paper fund">
      <form className="eval-modal__body" onSubmit={(event) => void submit(event)}>
        <header className="eval-drawer__header">
          <h3>New paper fund</h3>
          <button type="button" className="text-button" onClick={onClose}>
            Cancel
          </button>
        </header>
        <div className="models-grid models-grid--compact">
          <label htmlFor="ahf-fund-name">
            Fund name
            <input
              id="ahf-fund-name"
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="alpha"
              required
            />
          </label>
          <label htmlFor="ahf-fund-mandate">
            Mandate
            <select
              id="ahf-fund-mandate"
              value={mandate}
              onChange={(event) => setMandate(event.target.value)}
              required
            >
              <option value="">Select a mandate…</option>
              {mandates.map((entry) => (
                <option key={entry.name} value={entry.name}>
                  {entry.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label htmlFor="ahf-fund-tickers">
          Tickers
          <input
            id="ahf-fund-tickers"
            type="text"
            value={tickers}
            onChange={(event) => setTickers(event.target.value)}
            required
          />
        </label>
        <p className="panel-note">
          The mandate&apos;s capital and cadence apply. A mandate with a universe rule cannot be
          deployed as a paper fund yet — backtest it instead.
        </p>
        <button type="submit" className="auth-button" disabled={busy || name === "" || mandate === ""}>
          Deploy fund
        </button>
      </form>
    </div>
  );
}

function ApprovalModal({
  preview,
  onApprove,
  onReject,
}: {
  preview: Preview;
  onApprove: () => void;
  onReject: () => void;
}) {
  return (
    <div
      className="eval-modal"
      role="dialog"
      aria-modal="true"
      aria-label={`Approve session ${preview.session}`}
    >
      <div className="eval-modal__body">
        <header className="eval-drawer__header">
          <h3>Approve session {preview.session}</h3>
          <button type="button" className="text-button" onClick={onReject}>
            Reject
          </button>
        </header>
        <p className="panel-note">
          Computed against a copy of {preview.fund}; nothing has touched the live ledger. NAV{" "}
          {formatMoney(preview.nav)} · cash {formatMoney(preview.cash)}
          {preview.stopped_out ? " · drawdown stop-out: sells everything next session" : ""}
          {preview.halved ? " · halved book" : ""}
        </p>

        <h4>Proposed orders</h4>
        {preview.orders.length === 0 ? (
          <p className="panel-note">No orders this session (mark only).</p>
        ) : (
          <div className="table-scroll" role="region" aria-label="Proposed orders" tabIndex={0}>
            <table className="judgment-table">
              <thead>
                <tr>
                  <th scope="col">Ticker</th>
                  <th scope="col">Side</th>
                  <th scope="col">Shares</th>
                  <th scope="col">Reference</th>
                  <th scope="col">Notional</th>
                </tr>
              </thead>
              <tbody>
                {preview.orders.map((order, index) => (
                  <tr key={`${order.ticker}-${index}`}>
                    <th scope="row">{order.ticker}</th>
                    <td>{order.side}</td>
                    <td>{order.quantity}</td>
                    <td>{formatMoney(order.price)}</td>
                    <td>{formatMoney(order.notional)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <h4>Deterministic risk gates</h4>
        {preview.risk_checks.length === 0 ? (
          <p className="panel-note">No limit fired; the book was inside every bound.</p>
        ) : (
          <ul className="ahf-clamps">
            {preview.risk_checks.map((check, index) => (
              <li key={`${check.limit}-${index}`}>
                <span
                  className={`status-badge status-badge--${check.passed ? "ok" : "danger"}`}
                >
                  {check.limit}
                </span>{" "}
                {clampLabel(check.limit)} — {formatPct(check.before)} → {formatPct(check.after)}
              </li>
            ))}
          </ul>
        )}

        <h4>Agent reasoning</h4>
        {preview.reasoning.length === 0 ? (
          <p className="panel-note">No analyst views this session.</p>
        ) : (
          <details className="ahf-details">
            <summary>{preview.reasoning.length} views</summary>
            <ul className="ahf-signals">
              {preview.reasoning.map((entry, index) => (
                <li key={`${entry.model_name}-${entry.ticker}-${index}`}>
                  <strong>{entry.model_name}</strong> · {entry.ticker} · {entry.value.toFixed(2)}
                  {entry.reasoning && <p>{entry.reasoning}</p>}
                </li>
              ))}
            </ul>
          </details>
        )}

        <div className="ahf-actions">
          <button type="button" className="auth-button" onClick={onApprove}>
            Approve &amp; commit
          </button>
          <button type="button" className="text-button" onClick={onReject}>
            Reject
          </button>
        </div>
      </div>
    </div>
  );
}
