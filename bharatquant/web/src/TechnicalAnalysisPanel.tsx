import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  parseBatchAnalysis,
  parseVendorChains,
} from "./analysisContract";
import type {
  AgentTraceEntry,
  AnalysisRun,
  BatchAnalysis,
  BatchAnalysisItem,
} from "./analysisContract";
import WorkingMemoryPanel from "./WorkingMemoryPanel";

type ChainsState =
  | { state: "loading" | "error" }
  | { state: "ready"; chains: ReturnType<typeof parseVendorChains> };

function scoreLabel(value: number | null): string {
  return value === null ? "n/a" : value.toFixed(2);
}

const SPECIALIST_LABELS: Record<string, string> = {
  news: "News context",
  market_regime: "Market regime",
  fundamental_events: "Fundamental & events",
};

function specialistLabel(agent: string): string {
  return SPECIALIST_LABELS[agent] ?? agent;
}

function statusClass(status: AgentTraceEntry["status"]): string {
  return `trace-status trace-status--${status}`;
}

/**
 * Renders the multi-agent orchestration for one analysed symbol.
 *
 * This is the visible part of the framework: each agent's role and what it
 * actually contributed. A degraded LLM agent is shown as degraded rather than
 * hidden, because "the debate did not run" is information the owner needs.
 */
function AgentFlow({ entries }: { entries: AgentTraceEntry[] }) {
  if (entries.length === 0) {
    return null;
  }
  return (
    <ol className="agent-flow" aria-label="Agent contributions">
      {entries.map((entry) => (
        <li key={entry.node} className="agent-flow-step">
          <span className={statusClass(entry.status)}>{entry.status}</span>
          <strong>{entry.role}</strong>
          <span className="agent-flow-summary">{entry.summary}</span>
        </li>
      ))}
    </ol>
  );
}

function SymbolResult({ item }: { item: BatchAnalysisItem }) {
  const [expanded, setExpanded] = useState(false);

  if (!item.ok || item.analysis === null) {
    return (
      <li className="batch-item batch-item--failed">
        <div className="batch-item-head">
          <strong>{item.requested}</strong>
          <span className="status-badge status-badge--blocked">not analysed</span>
        </div>
        <p className="batch-error">{item.error}</p>
      </li>
    );
  }

  const run: AnalysisRun = item.analysis;
  const signal = run.plan ? run.plan.signal : "HOLD";
  return (
    <li className="batch-item">
      <div className="batch-item-head">
        <strong>{run.symbol}</strong>
        <span
          className={`status-badge ${
            signal === "BUY"
              ? "status-badge--allow"
              : "status-badge--provisional"
          }`}
        >
          {signal}
        </span>
        <span className="batch-composite">
          composite {scoreLabel(run.composite)}
        </span>
      </div>

      <dl className="analysis-scores">
        <div>
          <dt>Trend</dt>
          <dd>{scoreLabel(run.sub_scores.trend)}</dd>
        </div>
        <div>
          <dt>Momentum</dt>
          <dd>{scoreLabel(run.sub_scores.momentum)}</dd>
        </div>
        <div>
          <dt>Volatility</dt>
          <dd>{scoreLabel(run.sub_scores.volatility)}</dd>
        </div>
        <div>
          <dt>Structure</dt>
          <dd>{scoreLabel(run.sub_scores.structure)}</dd>
        </div>
      </dl>

      {run.plan ? (
        <dl className="analysis-plan-inline">
          <div>
            <dt>Entry</dt>
            <dd>{run.plan.entry_price}</dd>
          </div>
          <div>
            <dt>Stop</dt>
            <dd>{run.plan.stop_loss}</dd>
          </div>
          <div>
            <dt>Target</dt>
            <dd>{run.plan.target_price}</dd>
          </div>
          <div>
            <dt>R:R</dt>
            <dd>{run.plan.reward_risk}</dd>
          </div>
          <div>
            <dt>Qty</dt>
            <dd>{run.plan.suggested_quantity}</dd>
          </div>
        </dl>
      ) : (
        <p className="batch-veto">
          No plan. {run.vetoes.join(", ") || "No deterministic long case."}
        </p>
      )}

      {run.final_gate !== null && (
        <p
          className={`batch-gate ${
            run.final_gate.execute ? "batch-gate--allow" : "batch-gate--blocked"
          }`}
        >
          Final gate: <strong>{run.final_gate.action}</strong>{" "}
          {run.final_gate.execute ? "execute" : "blocked"} ·{" "}
          {run.final_gate.reason} ({run.final_gate.source} /{" "}
          {run.final_gate.model})
        </p>
      )}

      {run.evidence !== null && (
        <p className="panel-note">
          Evidence: {run.evidence.verified_claims} verified,{" "}
          {run.evidence.unverified_claims} unverified claim(s) via{" "}
          {run.evidence.decision_source}
          {run.evidence.model !== null ? ` / ${run.evidence.model}` : ""}
          {run.evidence.missing_evidence_ids.length > 0
            ? ` · missing ${run.evidence.missing_evidence_ids.join(", ")}`
            : ""}
        </p>
      )}

      <p className="batch-provenance">
        {run.data_provenance.is_realtime ? "real-time" : "delayed"} ·{" "}
        {run.mode === "RULES_ONLY" ? "rules only" : "with model debate"} ·{" "}
        {run.agents.length} agents
      </p>

      <button
        type="button"
        className="batch-expand"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        {expanded ? "Hide agents" : "Show agents"}
      </button>

      {expanded && (
        <div className="batch-detail">
          <AgentFlow entries={run.agents} />
          {run.run_id !== undefined && <WorkingMemoryPanel runId={run.run_id} />}
          <ul className="agent-tools">
            {run.facts.map((fact) => (
              <li key={fact}>{fact}</li>
            ))}
          </ul>
          {run.specialists.length > 0 && (
            <div className="specialist-briefs">
              <h3>Specialist briefs</h3>
              {run.specialists.map((brief) => (
                <article key={brief.agent} className="specialist-brief">
                  <div className="specialist-brief-head">
                    <strong>{specialistLabel(brief.agent)}</strong>
                    <span
                      className={`trace-status trace-status--${
                        brief.degraded ? "degraded" : "ok"
                      }`}
                    >
                      {brief.degraded ? "degraded" : "ok"}
                    </span>
                    <span className="prompt-meta">{brief.source}</span>
                  </div>
                  <ul className="agent-tools">
                    {brief.facts.map((fact) => (
                      <li key={fact}>{fact}</li>
                    ))}
                  </ul>
                  {brief.claims.length > 0 && (
                    <p className="prompt-meta">
                      Claims:{" "}
                      {brief.claims.filter((claim) => claim.verified).length}/
                      {brief.claims.length} verified
                    </p>
                  )}
                </article>
              ))}
            </div>
          )}
        </div>
      )}
    </li>
  );
}

export default function TechnicalAnalysisPanel({
  csrfToken,
}: {
  csrfToken: string;
}) {
  const [chains, setChains] = useState<ChainsState>({ state: "loading" });
  const [retry, setRetry] = useState(0);
  const [symbols, setSymbols] = useState("NSE:RELIANCE, NSE:TCS, NSE:HDFCBANK");
  const [timeframe, setTimeframe] = useState<"swing" | "intraday">("swing");
  const [equityRupees, setEquityRupees] = useState("1000000");
  const [cashRupees, setCashRupees] = useState("500000");
  const [running, setRunning] = useState(false);
  const [batch, setBatch] = useState<BatchAnalysis | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const loadChains = useCallback(async (signal: AbortSignal) => {
    try {
      const response = await fetch("/api/v1/analysis/vendors", {
        cache: "no-store",
        signal,
      });
      if (!response.ok) {
        throw new Error("Vendor status unavailable");
      }
      setChains({ state: "ready", chains: parseVendorChains(await response.json()) });
    } catch {
      if (!signal.aborted) {
        setChains({ state: "error" });
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadChains(controller.signal);
    return () => controller.abort();
  }, [loadChains, retry]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setFailure(null);
    setRunning(true);
    try {
      const equity = Number(equityRupees);
      const cash = Number(cashRupees);
      if (!Number.isFinite(equity) || !Number.isFinite(cash)) {
        throw new Error("Equity and cash must be numbers.");
      }
      const response = await fetch("/api/v1/analysis/batch", {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          symbols,
          timeframe,
          // The form speaks rupees; the API speaks paise.
          equity_paise: Math.round(equity * 100),
          available_cash_paise: Math.round(cash * 100),
          risk_basis_points: 100,
        }),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(
          (detail && typeof detail.detail === "string" && detail.detail) ||
            `Analysis failed (${response.status})`,
        );
      }
      setBatch(parseBatchAnalysis(await response.json()));
    } catch (error) {
      setBatch(null);
      setFailure(error instanceof Error ? error.message : "Analysis failed");
    } finally {
      setRunning(false);
    }
  };

  const dailyVendors =
    chains.state === "ready"
      ? chains.chains.chains.find((entry) => entry.domain === "bars.daily")
      : undefined;

  return (
    <section className="ta-panel" aria-labelledby="ta-title">
      <header className="page-header">
        <div>
          <p className="eyebrow">TECHNICAL ANALYSIS</p>
          <h1 id="ta-title">Multi-agent technical analysis</h1>
          <p className="intro">
            Submit one or more stocks. A technical analyst scores the chart, bull
            and bear advocates argue it, a judge weighs both, a risk analyst
            states what invalidates it, and a deterministic sizer builds the plan.
          </p>
        </div>
        <span className="status-badge status-badge--provisional">PAPER ONLY</span>
      </header>

      <section className="mode-banner" aria-label="Technical analysis scope">
        <span aria-hidden="true">i</span>
        <div>
          <strong>Advisory plans, never orders</strong>
          <p>
            Every number is computed in code; the model only argues. A plan is
            recorded, not placed — there is no broker connection. Delayed data is
            labelled, and nothing here is investment advice.
          </p>
        </div>
      </section>

      {chains.state === "ready" && (
        <p className="ta-vendor-note">
          Data: {dailyVendors ? dailyVendors.vendors.join(" → ") : "unavailable"} ·{" "}
          {chains.chains.kite_enabled
            ? "Kite enabled (read-only)"
            : "Kite disabled — delayed Yahoo data"}
        </p>
      )}
      {chains.state === "error" && (
        <p className="ta-vendor-note">
          Vendor status unavailable.{" "}
          <button type="button" onClick={() => setRetry((value) => value + 1)}>
            Retry
          </button>
        </p>
      )}

      <form className="ta-form" onSubmit={submit}>
        <label className="ta-form-symbols">
          Stocks (comma-separated, up to 10)
          <textarea
            value={symbols}
            onChange={(event) => setSymbols(event.target.value)}
            rows={2}
            placeholder="NSE:RELIANCE, NSE:TCS, NSE:HDFCBANK"
            required
          />
        </label>
        <div className="ta-form-row">
          <label>
            Timeframe
            <select
              value={timeframe}
              onChange={(event) =>
                setTimeframe(event.target.value === "intraday" ? "intraday" : "swing")
              }
            >
              <option value="swing">Swing (daily bars)</option>
              <option value="intraday">Intraday (15m bars)</option>
            </select>
          </label>
          <label>
            Equity (₹)
            <input
              value={equityRupees}
              onChange={(event) => setEquityRupees(event.target.value)}
              inputMode="numeric"
            />
          </label>
          <label>
            Cash (₹)
            <input
              value={cashRupees}
              onChange={(event) => setCashRupees(event.target.value)}
              inputMode="numeric"
            />
          </label>
          <button type="submit" disabled={running}>
            {running ? "Analysing…" : "Run analysis"}
          </button>
        </div>
      </form>

      {failure && <p className="agent-warning">{failure}</p>}

      {batch && (
        <>
          <p className="ta-summary">
            {batch.succeeded} of {batch.requested} analysed · {batch.failed} could
            not be · {batch.timeframe}
          </p>
          <ul className="batch-list">
            {batch.items.map((item) => (
              <SymbolResult key={`${item.requested}-${item.symbol}`} item={item} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
