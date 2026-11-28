import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import TableScroll from "./TableScroll";

import { parseBacktest, parseBacktests, parseStrategies } from "./backtestContract.ts";
import type { BacktestRun, BacktestTrade, StrategyRecord } from "./backtestContract.ts";

function formatPaise(value: string): string {
  return `₹${(Number(value) / 100).toLocaleString("en-IN", {
    maximumFractionDigits: 2,
  })}`;
}

function isoDate(value: string): string {
  return new Date(`${value}T00:00:00Z`).toISOString();
}

function defaultDate(daysAgo: number): string {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() - daysAgo);
  return date.toISOString().slice(0, 10);
}

export default function BacktestsPanel({ csrfToken }: { csrfToken: string }) {
  const [strategies, setStrategies] = useState<StrategyRecord[]>([]);
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [strategyId, setStrategyId] = useState("");
  const [start, setStart] = useState(defaultDate(120));
  const [end, setEnd] = useState(defaultDate(1));
  const [engine, setEngine] = useState<"FAST" | "EVENT">("FAST");
  const [capital, setCapital] = useState("100000");
  const [selected, setSelected] = useState<BacktestRun | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const [strategyResponse, runResponse] = await Promise.all([
          fetch("/api/v1/strategies", { cache: "no-store", signal: controller.signal }),
          fetch("/api/v1/backtests?limit=20", {
            cache: "no-store",
            signal: controller.signal,
          }),
        ]);
        if (!strategyResponse.ok || !runResponse.ok) {
          throw new Error("Strategy or backtest data is unavailable.");
        }
        const parsed = parseStrategies(await strategyResponse.json());
        setStrategies(parsed);
        setStrategyId((current) => current || parsed[0]?.strategy_id || "");
        setRuns(parseBacktests(await runResponse.json()));
      } catch {
        if (!controller.signal.aborted) {
          setError("Strategy or backtest data is unavailable.");
        }
      }
    }
    void load();
    return () => controller.abort();
  }, []);

  async function refreshRuns() {
    const response = await fetch("/api/v1/backtests?limit=20", { cache: "no-store" });
    if (response.ok) {
      setRuns(parseBacktests(await response.json()));
    }
  }

  async function runBacktest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const strategy = strategies.find((item) => item.strategy_id === strategyId);
    const capitalPaise = Math.round(Number(capital) * 100);
    if (
      strategy === undefined ||
      !Number.isFinite(capitalPaise) ||
      capitalPaise <= 0 ||
      start === "" ||
      end === ""
    ) {
      setError("Choose a strategy, dates and positive capital.");
      return;
    }
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await fetch(
        `/api/v1/strategies/${encodeURIComponent(strategy.strategy_id)}/${encodeURIComponent(strategy.version)}/backtest`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            start: isoDate(start),
            end: isoDate(end),
            engine,
            initial_capital_paise: capitalPaise,
          }),
        },
      );
      if (!response.ok) {
        const detail = (await response.json()) as { detail?: unknown };
        throw new Error(
          typeof detail.detail === "string" ? detail.detail : "Backtest failed.",
        );
      }
      const run = parseBacktest(await response.json());
      setSelected(run);
      setMessage(`${strategy.name} ${engine} run recorded.`);
      await refreshRuns();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Backtest failed.");
    } finally {
      setBusy(false);
    }
  }

  async function review(run: BacktestRun, decision: "ACCEPTED" | "REJECTED") {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await fetch(
        `/api/v1/backtests/${encodeURIComponent(run.run_id)}/review`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({ decision, note }),
        },
      );
      if (!response.ok) {
        throw new Error("The review decision failed.");
      }
      const updated = parseBacktest(await response.json());
      setSelected(updated);
      setMessage(`Run ${decision.toLowerCase()}.`);
      await refreshRuns();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Review failed.");
    } finally {
      setBusy(false);
    }
  }

  const strategy = strategies.find((item) => item.strategy_id === strategyId);

  return (
    <section className="settings-panel" aria-labelledby="backtests-title">
      <h2 id="backtests-title">Backtests &amp; strategy review</h2>
      <p className="panel-note">
        Strategies are immutable versions. Fast and event engines share the same
        rules, costs and point-in-time bounds; a parity mismatch is a bug, not a
        tuning opportunity. Reviews are append-only owner decisions.
      </p>
      {error !== "" && (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}
      {message !== "" && (
        <p className="panel-note" role="status">
          {message}
        </p>
      )}
      <form className="config-form" onSubmit={(event) => void runBacktest(event)}>
        <div className="models-grid models-grid--compact">
          <label htmlFor="backtest-strategy">
            Strategy
            <select
              id="backtest-strategy"
              value={strategyId}
              onChange={(event) => setStrategyId(event.target.value)}
            >
              {strategies.map((item) => (
                <option key={`${item.strategy_id}:${item.version}`} value={item.strategy_id}>
                  {item.name} v{item.version}
                  {item.is_seed ? " · seed" : ""}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor="backtest-start">
            Start
            <input
              id="backtest-start"
              type="date"
              value={start}
              onChange={(event) => setStart(event.target.value)}
            />
          </label>
          <label htmlFor="backtest-end">
            End
            <input
              id="backtest-end"
              type="date"
              value={end}
              onChange={(event) => setEnd(event.target.value)}
            />
          </label>
          <label htmlFor="backtest-engine">
            Engine
            <select
              id="backtest-engine"
              value={engine}
              onChange={(event) => setEngine(event.target.value as "FAST" | "EVENT")}
            >
              <option value="FAST">FAST · precomputed indicators</option>
              <option value="EVENT">EVENT · bar-by-bar</option>
            </select>
          </label>
          <label htmlFor="backtest-capital">
            Capital (₹)
            <input
              id="backtest-capital"
              type="number"
              min="1"
              step="1"
              value={capital}
              onChange={(event) => setCapital(event.target.value)}
            />
          </label>
        </div>
        <div className="strategy-examples">
          <button type="submit" className="auth-button" disabled={busy}>
            {busy ? "Running..." : "Run backtest"}
          </button>
        </div>
      </form>

      {strategy !== undefined && (
        <details className="strategy-result">
          <summary>
            {strategy.name} v{strategy.version} · {strategy.lifecycle} ·{" "}
            {strategy.content_hash.slice(0, 12)}
          </summary>
          <pre className="working-memory-json">
            {JSON.stringify(strategy.definition, null, 2)}
          </pre>
        </details>
      )}

      {selected !== null && (
        <div className="strategy-result">
          <h3>{selected.strategy_id} v{selected.version}</h3>
          <dl className="settings-facts">
            <div>
              <dt>Trades</dt>
              <dd>{selected.metrics.trades}</dd>
            </div>
            <div>
              <dt>Win rate</dt>
              <dd>
                {selected.metrics.win_rate === null
                  ? "n/a"
                  : `${Math.round(selected.metrics.win_rate * 100)}%`}
              </dd>
            </div>
            <div>
              <dt>Gross / charges / net</dt>
              <dd>
                {formatPaise(selected.metrics.gross_pnl_paise)} /{" "}
                {formatPaise(selected.metrics.charges_paise)} /{" "}
                {formatPaise(selected.metrics.net_pnl_paise)}
              </dd>
            </div>
            <div>
              <dt>Return / drawdown</dt>
              <dd>
                {(selected.metrics.return_bps / 100).toFixed(2)}% /{" "}
                {formatPaise(selected.metrics.max_drawdown_paise)}
              </dd>
            </div>
          </dl>
          {selected.trades.length > 0 && (
            <TableScroll label="Archived trades">
              <table className="judgment-table">
                <caption>Archived trades</caption>
                <thead>
                  <tr>
                    <th scope="col">Symbol</th>
                    <th scope="col">Entry</th>
                    <th scope="col">Exit</th>
                    <th scope="col">Qty</th>
                    <th scope="col">Net</th>
                    <th scope="col">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {selected.trades.map((trade: BacktestTrade, index) => (
                    <tr key={`${trade.symbol}-${index}`}>
                      <th scope="row">{trade.symbol}</th>
                      <td>{trade.entry_at.slice(0, 10)}</td>
                      <td>{trade.exit_at.slice(0, 10)}</td>
                      <td>{trade.quantity}</td>
                      <td>{formatPaise(trade.net_pnl_paise)}</td>
                      <td>{trade.exit_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
          )}
          <div className="models-grid models-grid--compact">
            <label htmlFor="backtest-note">
              Review note
              <input
                id="backtest-note"
                value={note}
                onChange={(event) => setNote(event.target.value)}
              />
            </label>
          </div>
          <div className="strategy-examples">
            <button
              type="button"
              className="text-button"
              disabled={busy}
              onClick={() => void review(selected, "ACCEPTED")}
            >
              Accept run
            </button>
            <button
              type="button"
              className="text-button"
              disabled={busy}
              onClick={() => void review(selected, "REJECTED")}
            >
              Reject run
            </button>
          </div>
          <p className="panel-note">
            Current review: {selected.review_decision ?? "unreviewed"}
            {selected.review_note ? ` · ${selected.review_note}` : ""}
          </p>
        </div>
      )}

      {runs.length > 0 && (
        <TableScroll label="Recent backtest runs">
          <table className="judgment-table">
            <caption>Recent backtest runs</caption>
            <thead>
              <tr>
                <th scope="col">Strategy</th>
                <th scope="col">Engine</th>
                <th scope="col">Trades</th>
                <th scope="col">Net</th>
                <th scope="col">Review</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id}>
                  <th scope="row">
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => {
                        setSelected(run);
                        setNote(run.review_note ?? "");
                      }}
                    >
                      {run.strategy_id} v{run.version}
                    </button>
                  </th>
                  <td>{run.engine}</td>
                  <td>{run.metrics.trades}</td>
                  <td>{formatPaise(run.metrics.net_pnl_paise)}</td>
                  <td>{run.review_decision ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </section>
  );
}
