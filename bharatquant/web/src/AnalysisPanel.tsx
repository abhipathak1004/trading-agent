import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  parseAnalysisRun,
  parseVendorChains,
} from "./analysisContract";
import type { AnalysisRun, VendorChains } from "./analysisContract";

type ChainsState =
  | { state: "loading" | "error" }
  | { state: "ready"; chains: VendorChains };

function scoreLabel(value: number | null): string {
  if (value === null) {
    return "n/a";
  }
  return value.toFixed(2);
}

function provenanceLine(run: AnalysisRun): string {
  const data = run.data_provenance;
  return [
    `data: ${data.vendor_daily}`,
    data.is_realtime ? "real-time" : "delayed",
    run.mode === "RULES_ONLY" ? "rules only (no model)" : "with model debate",
  ].join(" · ");
}

export default function AnalysisPanel({ csrfToken }: { csrfToken: string }) {
  const [chains, setChains] = useState<ChainsState>({ state: "loading" });
  const [retry, setRetry] = useState(0);
  const [symbol, setSymbol] = useState("NSE:RELIANCE");
  const [timeframe, setTimeframe] = useState<"swing" | "intraday">("swing");
  const [equityRupees, setEquityRupees] = useState("1000000");
  const [cashRupees, setCashRupees] = useState("500000");
  const [running, setRunning] = useState(false);
  const [run, setRun] = useState<AnalysisRun | null>(null);
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
      const response = await fetch("/api/v1/analysis/run", {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          symbol,
          timeframe,
          // The API speaks paise; the form speaks rupees because that is what
          // a person types. Convert once, here.
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
      setRun(parseAnalysisRun(await response.json()));
    } catch (error) {
      setRun(null);
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
    <section className="agent-console" aria-labelledby="analysis-title">
      <header className="agent-head">
        <div>
          <h2 id="analysis-title">Technical analysis</h2>
          <p className="eyebrow">Records a plan. Never places an order.</p>
        </div>
      </header>

      {chains.state === "ready" && (
        <div className="agent-warning">
          Data: {dailyVendors ? dailyVendors.vendors.join(" → ") : "unavailable"} ·{" "}
          {chains.chains.kite_enabled
            ? "Kite enabled (read-only)"
            : "Kite disabled — delayed Yahoo data"}
        </div>
      )}
      {chains.state === "error" && (
        <div className="agent-warning">
          Vendor status unavailable.{" "}
          <button type="button" onClick={() => setRetry((value) => value + 1)}>
            Retry
          </button>
        </div>
      )}

      <form className="analysis-form" onSubmit={submit}>
        <label>
          Symbol
          <input
            value={symbol}
            onChange={(event) => setSymbol(event.target.value)}
            placeholder="NSE:RELIANCE"
            required
          />
        </label>
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
          {running ? "Analysing…" : "Analyse"}
        </button>
      </form>

      {failure && <p className="agent-warning">{failure}</p>}

      {run && (
        <div className="analysis-result">
          <p className="eyebrow">{provenanceLine(run)}</p>

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

          <ul className="agent-tools">
            {run.facts.map((fact) => (
              <li key={fact}>{fact}</li>
            ))}
          </ul>

          {run.plan ? (
            <div className="analysis-plan">
              <h3>
                {run.plan.signal} · {run.plan.symbol} · {run.plan.product}
              </h3>
              <dl>
                <div>
                  <dt>Entry (LIMIT)</dt>
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
                  <dt>Quantity</dt>
                  <dd>{run.plan.suggested_quantity}</dd>
                </div>
              </dl>
              <p className="analysis-invalidation">
                <strong>Invalidation:</strong> {run.plan.invalidation_reason}
              </p>
            </div>
          ) : (
            <div className="agent-warning">
              No trade plan. {run.vetoes.join(", ") || "No deterministic long case."}
            </div>
          )}

          {run.notes.length > 0 && (
            <ul className="agent-tools">
              {run.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
