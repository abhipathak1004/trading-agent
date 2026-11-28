import { useCallback, useEffect, useMemo, useState } from "react";

import EquityCurve from "./EquityCurve.tsx";
import TableScroll from "./TableScroll.tsx";
import { isRecord } from "./contracts.ts";
import {
  parseAlphaBenchResult,
  parseArtifactList,
  parseReconcileResult,
  parseResearchEvent,
  parseResearchLabSettings,
  parseResearchRun,
  parseResearchSession,
} from "./researchLabContract.ts";
import type {
  AlphaBenchResult,
  ReconcileResult,
  ResearchArtifact,
  ResearchEvent,
  ResearchLabSettings,
  ResearchRun,
  ResearchSession,
} from "./researchLabContract.ts";

const TEMPLATES: { label: string; prompt: string }[] = [
  {
    label: "NIFTY 100 momentum bench",
    prompt:
      "Benchmark the 12-1 momentum factor on the current NIFTY 100 universe at 1, 5 and 20 day horizons. Report IC, rank IC and turnover, and note the survivorship bias in the universe.",
  },
  {
    label: "Pair backtest with NSE costs",
    prompt:
      "Backtest the SMA crossover strategy on the frozen bars, with NSE delivery costs, and report net return in basis points, trade count and the drawdown episodes.",
  },
  {
    label: "Walk-forward validation check",
    prompt:
      "Re-run the last completed backtest as a walk-forward study with three folds and a holdout, and report whether the out-of-sample return keeps its sign.",
  },
];

type Tab = "workbench" | "runs" | "artifacts" | "alpha";

function detail(value: unknown, fallback: string): string {
  if (isRecord(value) && typeof value.detail === "string") {
    return value.detail;
  }
  return fallback;
}

async function write(
  path: string,
  method: string,
  csrfToken: string,
  body?: unknown,
): Promise<Response> {
  const response = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(detail(await response.json().catch(() => null), `${method} ${path} failed (${response.status})`));
  }
  return response;
}

function badgeClass(word: string): string {
  if (word === "Verified") {
    return "status-badge status-badge--ok";
  }
  if (word === "Reduced") {
    return "status-badge status-badge--warn";
  }
  return "status-badge status-badge--danger";
}

export default function ResearchLabPanel({ csrfToken }: { csrfToken: string }) {
  const [tab, setTab] = useState<Tab>("workbench");
  const [sessions, setSessions] = useState<ResearchSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [runs, setRuns] = useState<ResearchRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [models, setModels] = useState<ResearchLabSettings | null>(null);
  const [prompt, setPrompt] = useState(TEMPLATES[0].prompt);
  const [events, setEvents] = useState<ResearchEvent[]>([]);
  const [streamRunId, setStreamRunId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshRuns = useCallback(async (target: string | null) => {
    if (!target) {
      setRuns([]);
      return;
    }
    const response = await fetch(
      `/api/v1/research/runs?session_id=${encodeURIComponent(target)}`,
      { cache: "no-store" },
    );
    if (!response.ok) {
      return;
    }
    const payload = (await response.json()) as { runs?: unknown[] };
    const parsed = (payload.runs ?? []).map(parseResearchRun);
    setRuns(parsed);
    setSelectedRunId((current) => current ?? parsed[0]?.run_id ?? null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [sessionResponse, modelResponse] = await Promise.all([
        fetch("/api/v1/research/sessions", { cache: "no-store" }),
        fetch("/api/v1/research/models", { cache: "no-store" }),
      ]);
      if (cancelled) {
        return;
      }
      if (modelResponse.ok) {
        setModels(parseResearchLabSettings(await modelResponse.json()));
      }
      if (!sessionResponse.ok) {
        setError(
          detail(
            await sessionResponse.json().catch(() => null),
            "Could not load research sessions",
          ),
        );
        return;
      }
      const payload = (await sessionResponse.json()) as { sessions?: unknown[] };
      const parsed = (payload.sessions ?? []).map(parseResearchSession);
      setSessions(parsed);
      if (parsed.length > 0) {
        setSessionId(parsed[0].session_id);
        await refreshRuns(parsed[0].session_id);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshRuns]);

  useEffect(() => {
    if (!sessionId || !streamRunId) {
      return;
    }
    const source = new EventSource(
      `/api/v1/research/sessions/${sessionId}/events?run_id=${encodeURIComponent(streamRunId)}`,
    );
    source.onmessage = (message: MessageEvent<string>) => {
      let parsed: ResearchEvent;
      try {
        parsed = parseResearchEvent(JSON.parse(message.data));
      } catch {
        return;
      }
      setEvents((current) =>
        current.some((entry) => entry.seq === parsed.seq) ? current : [...current, parsed],
      );
      if (parsed.done) {
        source.close();
        void refreshRuns(sessionId);
      }
    };
    source.onerror = () => source.close();
    return () => source.close();
  }, [sessionId, streamRunId, refreshRuns]);

  const verdict = useMemo(() => {
    const latest = [...events].reverse().find((event) => event.type === "evidence_verdict");
    if (!latest) {
      return { word: "Pending", detail: "No verdict yet" };
    }
    if (latest.passed) {
      return { word: "Verified", detail: "Every figure matched admitted evidence" };
    }
    return latest.reduced
      ? { word: "Reduced", detail: "Unverified figures were removed" }
      : { word: "Blocked", detail: "No figure could be verified" };
  }, [events]);

  const provenance = useMemo(
    () => events.filter((event) => event.type === "provenance"),
    [events],
  );

  async function createSession() {
    setBusy(true);
    setError(null);
    try {
      const response = await write("/api/v1/research/sessions", "POST", csrfToken, {
        title: `Research ${new Date().toISOString().slice(0, 16)}`,
      });
      const session = parseResearchSession(await response.json());
      setSessions((current) => [session, ...current]);
      setSessionId(session.session_id);
      setRuns([]);
      setSelectedRunId(null);
      setEvents([]);
      setStreamRunId(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create a session");
    } finally {
      setBusy(false);
    }
  }

  async function runPrompt() {
    if (!sessionId) {
      return;
    }
    setBusy(true);
    setError(null);
    setEvents([]);
    try {
      const response = await write(
        `/api/v1/research/sessions/${sessionId}/messages`,
        "POST",
        csrfToken,
        { prompt },
      );
      const payload = (await response.json()) as { run_id?: unknown };
      const runId = typeof payload.run_id === "string" ? payload.run_id : null;
      setStreamRunId(runId);
      await refreshRuns(sessionId);
      setTab("workbench");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start the run");
    } finally {
      setBusy(false);
    }
  }

  async function stopRun() {
    if (!sessionId) {
      return;
    }
    try {
      await write(
        `/api/v1/research/sessions/${sessionId}/cancel`,
        "POST",
        csrfToken,
      );
      await refreshRuns(sessionId);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not cancel");
    }
  }

  async function saveArtifact() {
    const run = runs.find((entry) => entry.run_id === streamRunId) ?? runs[0];
    const text =
      run && typeof run.metrics.text === "string" ? run.metrics.text : verdict.detail;
    try {
      await write("/api/v1/research/artifacts", "POST", csrfToken, {
        slug: `research-${(run?.run_id ?? "session").slice(0, 12)}`,
        title: prompt.slice(0, 120) || "Research conclusion",
        body_md: text || "No verified report was produced.",
        linked_run_ids: run ? [run.run_id] : [],
        evidence_refs: [],
      });
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save the artifact");
    }
  }

  return (
    <div className="settings-panel">
      <div className="tab-bar" role="tablist" aria-label="Research Lab sections">
        {(
          [
            ["workbench", "Workbench"],
            ["runs", "Run Detail"],
            ["artifacts", "Artifacts"],
            ["alpha", "Alpha Bench"],
          ] as [Tab, string][]
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={`tab-button${tab === key ? " tab-button--active" : ""}`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {error ? <p className="panel-note panel-note--error">{error}</p> : null}

      {tab === "workbench" ? (
        <section aria-label="Research workbench">
          <div className="mode-banner" aria-label="Active models">
            <strong>Models</strong>{" "}
            {models ? (
              <>
                planner <code>{models.routes.research_lab_planner?.tier ?? "—"}</code>
                {" · "}synthesizer{" "}
                <code>{models.routes.research_lab_synthesizer?.tier ?? "—"}</code>
                {" · "}auditor{" "}
                <code>{models.routes.research_lab_evidence_auditor?.tier ?? "—"}</code>
              </>
            ) : (
              "loading…"
            )}
            <span className="mode-banner__spacer" />
            <span className={badgeClass(verdict.word)} title={verdict.detail}>
              Evidence: {verdict.word}
            </span>
          </div>

          <div className="field">
            <label htmlFor="research-prompt">Research request</label>
            <textarea
              id="research-prompt"
              value={prompt}
              rows={4}
              maxLength={20000}
              onChange={(event) => setPrompt(event.target.value)}
            />
          </div>

          <div className="panel-note">
            {TEMPLATES.map((template) => (
              <button
                key={template.label}
                type="button"
                className="tab-button"
                onClick={() => setPrompt(template.prompt)}
              >
                {template.label}
              </button>
            ))}
          </div>

          <div className="field">
            <label htmlFor="research-session">Session</label>
            <select
              id="research-session"
              value={sessionId ?? ""}
              onChange={(event) => {
                const next = event.target.value;
                setSessionId(next);
                setEvents([]);
                setStreamRunId(null);
                setSelectedRunId(null);
                void refreshRuns(next);
              }}
            >
              {sessions.length === 0 ? <option value="">(no session)</option> : null}
              {sessions.map((entry) => (
                <option key={entry.session_id} value={entry.session_id}>
                  {entry.title}
                </option>
              ))}
            </select>
          </div>

          <div className="panel-note">
            <button
              type="button"
              className="tab-button tab-button--active"
              disabled={busy || !sessionId}
              onClick={() => void runPrompt()}
            >
              Run
            </button>
            <button
              type="button"
              className="tab-button"
              disabled={!sessionId}
              onClick={() => void stopRun()}
            >
              Stop
            </button>
            <button type="button" className="tab-button" onClick={() => void createSession()}>
              New session
            </button>
            <button
              type="button"
              className="tab-button"
              disabled={!runs.length}
              onClick={() => void saveArtifact()}
            >
              Save as artifact
            </button>
          </div>

          {provenance.length > 0 ? (
            <p className="panel-note">
              Provenance:{" "}
              {provenance.map((event) => (
                <code key={event.seq}>
                  {event.run_id ?? "run"} · {event.metrics.join(", ") || "metrics"}{" "}
                </code>
              ))}
            </p>
          ) : null}

          <TableScroll label="Research run timeline">
            <table className="judgment-table">
              <caption>Streamed events, newest last</caption>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">Type</th>
                  <th scope="col">Detail</th>
                </tr>
              </thead>
              <tbody>
                {events.length === 0 ? (
                  <tr>
                    <td colSpan={3}>No events yet.</td>
                  </tr>
                ) : (
                  events.map((event) => (
                    <tr key={event.seq}>
                      <td>{event.seq}</td>
                      <td>{event.type}</td>
                      <td>
                        {event.tool ?? event.detail ?? (event.passed === null ? "" : event.passed ? "verified" : "rejected")}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </TableScroll>
        </section>
      ) : null}

      {tab === "runs" ? (
        <RunDetail
          runs={runs}
          selectedRunId={selectedRunId}
          onSelect={setSelectedRunId}
          csrfToken={csrfToken}
          onRefresh={() => void refreshRuns(sessionId)}
        />
      ) : null}

      {tab === "artifacts" ? <ArtifactsTab csrfToken={csrfToken} /> : null}

      {tab === "alpha" ? <AlphaBenchTab csrfToken={csrfToken} /> : null}
    </div>
  );
}

type RunPayload = {
  run: ResearchRun;
  equity: number[];
  episodes: Record<string, unknown>[];
};

function RunDetail({
  runs,
  selectedRunId,
  onSelect,
  csrfToken,
  onRefresh,
}: {
  runs: ResearchRun[];
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
  csrfToken: string;
  onRefresh: () => void;
}) {
  const [payload, setPayload] = useState<RunPayload | null>(null);
  const [folds, setFolds] = useState("3");
  const [holdout, setHoldout] = useState("0");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedRunId) {
      setPayload(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      const response = await fetch(`/api/v1/research/runs/${selectedRunId}`, {
        cache: "no-store",
      });
      if (!response.ok || cancelled) {
        return;
      }
      const raw: unknown = await response.json();
      const run = parseResearchRun(raw);
      const equityRaw = isRecord(raw) ? raw["equity.json"] : null;
      const equity =
        isRecord(equityRaw) && Array.isArray(equityRaw.equity)
          ? equityRaw.equity.map((value) => Number(value)).filter(Number.isFinite)
          : [];
      const riskRaw = isRecord(raw) ? raw["risk_xray.json"] : null;
      const episodes =
        isRecord(riskRaw) && Array.isArray(riskRaw.episodes)
          ? riskRaw.episodes.filter(isRecord)
          : [];
      setPayload({ run, equity, episodes });
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedRunId]);

  async function replay() {
    if (!selectedRunId) {
      return;
    }
    setError(null);
    try {
      const foldCount = Number.parseInt(folds, 10);
      const holdoutBars = Number.parseInt(holdout, 10);
      await write(`/api/v1/research/runs/${selectedRunId}/replay`, "POST", csrfToken, {
        folds: Number.isFinite(foldCount) && foldCount >= 2 ? foldCount : null,
        holdout_bars: Number.isFinite(holdoutBars) ? holdoutBars : null,
      });
      onRefresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Replay failed");
    }
  }

  const run = payload?.run ?? null;
  const metrics = run?.metrics ?? {};

  return (
    <section aria-label="Run detail">
      {runs.length === 0 ? (
        <p className="panel-note">No runs yet. Start one from the Workbench.</p>
      ) : (
        <div className="field">
          <label htmlFor="research-run">Run</label>
          <select
            id="research-run"
            value={selectedRunId ?? ""}
            onChange={(event) => onSelect(event.target.value)}
          >
            {runs.map((entry) => (
              <option key={entry.run_id} value={entry.run_id}>
                {entry.run_id.slice(0, 12)} · {entry.status}
              </option>
            ))}
          </select>
        </div>
      )}

      {error ? <p className="panel-note panel-note--error">{error}</p> : null}

      {run ? (
        <>
          <p className="panel-note">
            Status <strong>{run.status}</strong>
            {run.parent_run_id ? <> · parent {run.parent_run_id.slice(0, 12)}</> : null}
            {run.data_manifest_sha256 ? (
              <>
                {" "}
                · manifest <code>{run.data_manifest_sha256.slice(0, 16)}…</code>
              </>
            ) : null}
          </p>

          <div className="dashboard-grid dashboard-grid--single">
            <div className="status-badge status-badge--muted">
              return {String(metrics.return_bps ?? "—")} bps
            </div>
            <div className="status-badge status-badge--muted">
              trades {String(metrics.trades ?? "—")}
            </div>
            <div className="status-badge status-badge--muted">
              sharpe {String(metrics.sharpe ?? "undefined")}
            </div>
            <div className="status-badge status-badge--muted">
              sortino {String(metrics.sortino ?? "undefined")}
            </div>
            <div className="status-badge status-badge--muted">
              profit factor {String(metrics.profit_factor ?? "undefined")}
            </div>
          </div>

          {payload && payload.equity.length > 1 ? (
            <EquityCurve
              dates={payload.equity.map((_, index) => String(index + 1))}
              series={[
                {
                  label: "Equity (paise)",
                  values: payload.equity,
                  tone: "accent",
                },
              ]}
              title="Equity after each trade"
              labelX="Trade"
            />
          ) : (
            <p className="panel-note">This run has no equity curve.</p>
          )}

          <h3>Risk X-Ray</h3>
          {payload && payload.episodes.length > 0 ? (
            <TableScroll label="Drawdown episodes">
              <table className="judgment-table">
                <caption>Deepest observed drawdowns, plus any open one</caption>
                <thead>
                  <tr>
                    <th scope="col">Depth %</th>
                    <th scope="col">Duration (bars)</th>
                    <th scope="col">Open</th>
                  </tr>
                </thead>
                <tbody>
                  {payload.episodes.map((episode, index) => (
                    <tr key={index}>
                      <td>{Number(episode.depth_pct ?? 0).toFixed(2)}</td>
                      <td>{String(episode.duration_bars ?? "—")}</td>
                      <td>{episode.is_open === true ? "yes" : "no"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
          ) : (
            <p className="panel-note">No drawdown episodes recorded.</p>
          )}

          <div className="field">
            <label htmlFor="replay-folds">Replay as walk-forward</label>
            <input
              id="replay-folds"
              type="number"
              min={2}
              max={20}
              value={folds}
              onChange={(event) => setFolds(event.target.value)}
            />
            <input
              type="number"
              min={0}
              max={500}
              value={holdout}
              onChange={(event) => setHoldout(event.target.value)}
              aria-label="Holdout bars"
            />
            <button type="button" className="tab-button" onClick={() => void replay()}>
              Run replay
            </button>
          </div>

          <p className="panel-note">
            <a href={`/api/v1/research/runs/${run.run_id}/export.csv?kind=trades`}>
              Download trades CSV
            </a>
            {" · "}
            <a href={`/api/v1/research/runs/${run.run_id}/export.csv?kind=folds`}>
              Download folds CSV
            </a>
          </p>
        </>
      ) : null}
    </section>
  );
}

function ArtifactsTab({ csrfToken }: { csrfToken: string }) {
  const [query, setQuery] = useState("momentum");
  const [artifacts, setArtifacts] = useState<ResearchArtifact[]>([]);
  const [selected, setSelected] = useState<ResearchArtifact | null>(null);
  const [revA, setRevA] = useState("1");
  const [revB, setRevB] = useState("2");
  const [diff, setDiff] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function search() {
    setError(null);
    try {
      const response = await fetch(
        `/api/v1/research/artifacts?q=${encodeURIComponent(query)}`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        throw new Error(`Search failed (${response.status})`);
      }
      const parsed = parseArtifactList(await response.json());
      setArtifacts(parsed.artifacts);
      setSelected(parsed.artifacts[0] ?? null);
      setDiff(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Search failed");
    }
  }

  async function compare() {
    if (!selected) {
      return;
    }
    setError(null);
    try {
      const response = await fetch(
        `/api/v1/research/artifacts/${selected.artifact_id}/diff?rev_a=${revA}&rev_b=${revB}`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        throw new Error(`Diff failed (${response.status})`);
      }
      const payload = (await response.json()) as { unified?: unknown };
      setDiff(typeof payload.unified === "string" ? payload.unified : "(no textual difference)");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Diff failed");
    }
  }

  return (
    <section aria-label="Versioned research artifacts">
      <div className="field">
        <label htmlFor="artifact-query">Search (FTS5)</label>
        <input
          id="artifact-query"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <button type="button" className="tab-button" onClick={() => void search()}>
          Search
        </button>
      </div>

      {error ? <p className="panel-note panel-note--error">{error}</p> : null}

      {artifacts.length > 0 ? (
        <TableScroll label="Research artifacts">
          <table className="judgment-table">
            <caption>Latest revision per slug</caption>
            <thead>
              <tr>
                <th scope="col">Slug</th>
                <th scope="col">Rev</th>
                <th scope="col">Title</th>
              </tr>
            </thead>
            <tbody>
              {artifacts.map((artifact) => (
                <tr key={artifact.artifact_id}>
                  <td>
                    <button
                      type="button"
                      className="tab-button"
                      onClick={() => {
                        setSelected(artifact);
                        setDiff(null);
                      }}
                    >
                      {artifact.slug}
                    </button>
                  </td>
                  <td>{artifact.revision}</td>
                  <td>{artifact.title}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      ) : (
        <p className="panel-note">No artifacts matched.</p>
      )}

      {selected ? (
        <>
          <h3>
            {selected.title} (r{selected.revision})
          </h3>
          <pre className="panel-note">{selected.body_md}</pre>
          <div className="field">
            <label htmlFor="artifact-rev-a">Compare revisions</label>
            <input
              id="artifact-rev-a"
              type="number"
              min={1}
              value={revA}
              onChange={(event) => setRevA(event.target.value)}
            />
            <input
              type="number"
              min={1}
              value={revB}
              onChange={(event) => setRevB(event.target.value)}
              aria-label="Revision B"
            />
            <button type="button" className="tab-button" onClick={() => void compare()}>
              Diff
            </button>
          </div>
          {diff ? <pre className="panel-note">{diff}</pre> : null}
          <p className="panel-note">
            <a href={`/api/v1/research/artifacts/${selected.artifact_id}/export?format=md`}>
              Download Markdown
            </a>
            {" · "}
            <a href={`/api/v1/research/artifacts/${selected.artifact_id}/export?format=json`}>
              Download JSON
            </a>
            {" · "}
            <span>save a new revision from the Workbench ({csrfToken ? "writable" : "read only"})</span>
          </p>
        </>
      ) : null}
    </section>
  );
}

function samplePanel(): { bars: string; factor: string } {
  const symbols = ["S00", "S01", "S02", "S03", "S04", "S05"];
  const bars: Record<string, unknown>[] = [];
  const factor: Record<string, unknown>[] = [];
  for (let index = 0; index < symbols.length; index += 1) {
    for (let day = 0; day < 8; day += 1) {
      const stamp = new Date(Date.UTC(2026, 0, 1 + day)).toISOString();
      bars.push({
        symbol: symbols[index],
        ts: stamp,
        close: 10000 + index * 100 + day * (index + 1) * 20,
      });
      if (day < 7) {
        factor.push({ symbol: symbols[index], ts: stamp, factor_value: index + 1 });
      }
    }
  }
  return { bars: JSON.stringify(bars, null, 2), factor: JSON.stringify(factor, null, 2) };
}

function AlphaBenchTab({ csrfToken }: { csrfToken: string }) {
  const initial = useMemo(samplePanel, []);
  const [bars, setBars] = useState(initial.bars);
  const [factor, setFactor] = useState(initial.factor);
  const [horizons, setHorizons] = useState("1,5");
  const [result, setResult] = useState<AlphaBenchResult | null>(null);
  const [reconcile, setReconcile] = useState<ReconcileResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setError(null);
    try {
      const parsedHorizons = horizons
        .split(",")
        .map((item) => Number.parseInt(item.trim(), 10))
        .filter((item) => Number.isFinite(item) && item > 0);
      const response = await write("/api/v1/research/alpha/bench", "POST", csrfToken, {
        bars: JSON.parse(bars) as unknown,
        factor: JSON.parse(factor) as unknown,
        horizons: parsedHorizons,
        source: "inline",
        membership_is_current: true,
      });
      setResult(parseAlphaBenchResult(await response.json()));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Bench failed");
    }
  }

  async function reconcileDemo() {
    setError(null);
    try {
      const response = await write(
        "/api/v1/research/paper/reconcile",
        "POST",
        csrfToken,
        {
          initial_cash_paise: 2000000,
          fills: [
            {
              symbol: "AAA",
              side: "BUY",
              quantity: 100,
              price_paise: 10000,
              fees_paise: 1187,
            },
          ],
          ending_prices: { AAA: 10500 },
          reported_state: {
            cash_paise: 998813,
            fees_paise: 1187,
            realized_pnl_paise: 0,
            unrealized_pnl_paise: 50000,
            nav_paise: 2048813,
          },
        },
      );
      setReconcile(parseReconcileResult(await response.json()));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Reconcile failed");
    }
  }

  return (
    <section aria-label="Alpha benchmark">
      <div className="field">
        <label htmlFor="alpha-bars">Bars JSON</label>
        <textarea
          id="alpha-bars"
          rows={6}
          value={bars}
          onChange={(event) => setBars(event.target.value)}
        />
        <label htmlFor="alpha-factor">Factor JSON</label>
        <textarea
          id="alpha-factor"
          rows={6}
          value={factor}
          onChange={(event) => setFactor(event.target.value)}
        />
        <label htmlFor="alpha-horizons">Horizons (bars)</label>
        <input
          id="alpha-horizons"
          value={horizons}
          onChange={(event) => setHorizons(event.target.value)}
        />
        <button type="button" className="tab-button" onClick={() => void run()}>
          Run bench
        </button>
        <button type="button" className="tab-button" onClick={() => void reconcileDemo()}>
          Reconcile a paper ledger
        </button>
      </div>

      {error ? <p className="panel-note panel-note--error">{error}</p> : null}

      {result ? (
        <>
          {result.meta.survivorship_bias_warning ? (
            <p className="mode-banner" role="status">
              <strong>Survivorship bias:</strong> membership is the current
              snapshot, not point-in-time. Historical IC is biased upward.
            </p>
          ) : null}
          <TableScroll label="Alpha benchmark results">
            <table className="judgment-table">
              <caption>
                {result.meta.universe_size} names · source {result.meta.source}
              </caption>
              <thead>
                <tr>
                  <th scope="col">Horizon</th>
                  <th scope="col">IC</th>
                  <th scope="col">Rank IC</th>
                  <th scope="col">ICIR</th>
                  <th scope="col">Turnover</th>
                </tr>
              </thead>
              <tbody>
                {result.horizons.map((row) => (
                  <tr key={row.horizon_bars}>
                    <td>{row.horizon_bars}d</td>
                    <td>{row.ic_mean ?? "—"}</td>
                    <td>{row.rank_ic_mean ?? "—"}</td>
                    <td>{row.icir ?? "undefined"}</td>
                    <td>{row.turnover ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        </>
      ) : (
        <p className="panel-note">No benchmark has been run yet.</p>
      )}

      {reconcile ? (
        <p className={reconcile.passed ? "status-badge status-badge--ok" : "status-badge status-badge--danger"}>
          paper_trade_reconcile: {reconcile.passed ? "passed" : `${reconcile.discrepancies.length} discrepancy`}
        </p>
      ) : null}
    </section>
  );
}

export function ResearchLabAgentsPanel({ csrfToken }: { csrfToken: string }) {
  const [settings, setSettings] = useState<ResearchLabSettings | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const response = await fetch("/api/v1/research/models", { cache: "no-store" });
      if (response.ok && !cancelled) {
        setSettings(parseResearchLabSettings(await response.json()));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function save(tier: string) {
    if (!settings) {
      return;
    }
    const planner = isRecord(settings.research_lab.planner)
      ? settings.research_lab.planner
      : {};
    try {
      const response = await write("/api/v1/research/models", "PUT", csrfToken, {
        research_lab: { ...settings.research_lab, planner: { ...planner, tier } },
      });
      setSettings(parseResearchLabSettings(await response.json()));
      setMessage("Saved.");
    } catch (caught) {
      setMessage(caught instanceof Error ? caught.message : "Could not save");
    }
  }

  return (
    <section aria-label="Research Lab agents">
      <h3>Research Lab Agents</h3>
      {settings ? (
        <TableScroll label="Research Lab agent routes">
          <table className="judgment-table">
            <caption>Resolved through the same Settings store as every other agent</caption>
            <thead>
              <tr>
                <th scope="col">Agent</th>
                <th scope="col">Tier</th>
                <th scope="col">Model</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(settings.routes).map(([agentId, route]) => (
                <tr key={agentId}>
                  <th scope="row">{agentId.replace("research_lab_", "")}</th>
                  <td>{route.tier}</td>
                  <td>{route.model ?? "tier default"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      ) : (
        <p className="panel-note">Loading…</p>
      )}
      <div className="panel-note">
        <label htmlFor="research-planner-tier">Planner tier</label>
        <select
          id="research-planner-tier"
          value={settings?.routes.research_lab_planner?.tier ?? "reasoning"}
          onChange={(event) => void save(event.target.value)}
        >
          <option value="reasoning">reasoning</option>
          <option value="flash">flash</option>
        </select>
        {message ? <span> {message}</span> : null}
      </div>
    </section>
  );
}
