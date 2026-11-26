import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { parsePlaybookList } from "./playbookContract";
import type { Playbook } from "./playbookContract";
import { parseStrategyRun } from "./strategyContract";
import type { StrategyPlan, StrategyRun, StrategySymbol } from "./strategyContract";

type RunState =
  | { state: "idle" | "running" }
  | { state: "error"; message: string }
  | { state: "ready"; run: StrategyRun };

const EXAMPLES = [
  "Momentum: rank the names by trend and momentum, buy the strongest two, and skip anything with a weak R:R.",
  "Defensive: only take clean trend setups; avoid high-volatility names and keep one position.",
  "Swing: prefer volume-confirmed breakouts, hold at most two positions, and require a clear invalidation level.",
];

function rupeesToPaise(value: string): number | null {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) {
    return null;
  }
  return Math.round(amount * 100);
}

function formatPaise(value: number): string {
  return `₹${(value / 100).toLocaleString("en-IN", {
    maximumFractionDigits: 2,
  })}`;
}

export default function StrategyPanel({
  csrfToken,
  onStagePlan,
}: {
  csrfToken: string;
  onStagePlan?: (plan: StrategyPlan) => void;
}) {
  const [strategy, setStrategy] = useState(EXAMPLES[0]);
  const [symbols, setSymbols] = useState("NSE:RELIANCE, NSE:TCS, NSE:HDFCBANK");
  const [timeframe, setTimeframe] = useState<"swing" | "intraday">("swing");
  const [equity, setEquity] = useState("100000");
  const [cash, setCash] = useState("50000");
  const [riskBps, setRiskBps] = useState("100");
  const [maxPositions, setMaxPositions] = useState("2");
  const [playbooks, setPlaybooks] = useState<Playbook[]>([]);
  const [playbookId, setPlaybookId] = useState("");
  const [run, setRun] = useState<RunState>({ state: "idle" });

  useEffect(() => {
    const controller = new AbortController();
    async function loadPlaybooks() {
      try {
        const response = await fetch("/api/v1/memory/playbooks", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (response.ok) {
          setPlaybooks(parsePlaybookList(await response.json()));
        }
      } catch {
        // A playbook is optional; the strategy still runs without one.
      }
    }
    void loadPlaybooks();
    return () => controller.abort();
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const equityPaise = rupeesToPaise(equity);
    const cashPaise = rupeesToPaise(cash);
    const risk = Number(riskBps);
    const positions = Number(maxPositions);
    if (!strategy.trim() || !symbols.trim()) {
      setRun({ state: "error", message: "A strategy and at least one symbol are required." });
      return;
    }
    if (equityPaise === null || equityPaise <= 0 || cashPaise === null) {
      setRun({ state: "error", message: "Equity must be positive and cash non-negative." });
      return;
    }
    if (!Number.isInteger(risk) || risk < 0 || risk > 10_000) {
      setRun({ state: "error", message: "Risk must be 0–10,000 basis points." });
      return;
    }
    if (!Number.isInteger(positions) || positions < 1 || positions > 10) {
      setRun({ state: "error", message: "Max positions must be between 1 and 10." });
      return;
    }
    setRun({ state: "running" });
    try {
      const response = await fetch("/api/v1/strategy/run", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({
          strategy,
          symbols,
          timeframe,
          equity_paise: equityPaise,
          available_cash_paise: cashPaise,
          risk_basis_points: risk,
          max_positions: positions,
          playbook_id: playbookId || null,
        }),
      });
      if (!response.ok) {
        let message = "The strategy run failed.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setRun({ state: "error", message });
        return;
      }
      setRun({ state: "ready", run: parseStrategyRun(await response.json()) });
    } catch {
      setRun({
        state: "error",
        message: "The strategy run failed. Check the local service.",
      });
    }
  }

  return (
    <section className="settings-panel" aria-labelledby="strategy-title">
      <h2 id="strategy-title">Run a strategy through the Super Agent</h2>
      <p className="panel-note">
        The Super Agent receives your brief, runs the existing agents on each
        symbol, ranks the results, and applies a deterministic cash and position
        gate. It can downgrade a plan but cannot invent a BUY. No orders are
        placed.
      </p>

      <form className="config-form" onSubmit={(event) => void submit(event)}>
        <label htmlFor="strategy-brief">Strategy brief</label>
        <textarea
          id="strategy-brief"
          rows={5}
          maxLength={2000}
          value={strategy}
          onChange={(event) => setStrategy(event.target.value)}
        />
        <div className="strategy-examples">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="text-button"
              onClick={() => setStrategy(example)}
            >
              {example.split(":")[0]}
            </button>
          ))}
        </div>

        <label htmlFor="strategy-symbols">Symbols</label>
        <input
          id="strategy-symbols"
          type="text"
          value={symbols}
          onChange={(event) => setSymbols(event.target.value)}
          placeholder="NSE:RELIANCE, NSE:TCS"
        />

        <div className="models-grid models-grid--compact">
          <label htmlFor="strategy-timeframe">
            Timeframe
            <select
              id="strategy-timeframe"
              value={timeframe}
              onChange={(event) =>
                setTimeframe(event.target.value as "swing" | "intraday")
              }
            >
              <option value="swing">swing</option>
              <option value="intraday">intraday</option>
            </select>
          </label>
          <label htmlFor="strategy-equity">
            Equity (₹)
            <input
              id="strategy-equity"
              type="number"
              min="1"
              value={equity}
              onChange={(event) => setEquity(event.target.value)}
            />
          </label>
          <label htmlFor="strategy-cash">
            Available cash (₹)
            <input
              id="strategy-cash"
              type="number"
              min="0"
              value={cash}
              onChange={(event) => setCash(event.target.value)}
            />
          </label>
          <label htmlFor="strategy-risk">
            Risk per trade (bps)
            <input
              id="strategy-risk"
              type="number"
              min="0"
              max="10000"
              value={riskBps}
              onChange={(event) => setRiskBps(event.target.value)}
            />
          </label>
          <label htmlFor="strategy-positions">
            Max positions
            <input
              id="strategy-positions"
              type="number"
              min="1"
              max="10"
              value={maxPositions}
              onChange={(event) => setMaxPositions(event.target.value)}
            />
          </label>
          <label htmlFor="strategy-playbook">
            Playbook (optional)
            <select
              id="strategy-playbook"
              value={playbookId}
              onChange={(event) => setPlaybookId(event.target.value)}
            >
              <option value="">No playbook</option>
              {playbooks.map((playbook) => (
                <option key={playbook.playbook_id} value={playbook.playbook_id}>
                  {playbook.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        <button
          type="submit"
          className="auth-button"
          disabled={run.state === "running"}
        >
          {run.state === "running" ? "Running agents..." : "Run strategy"}
        </button>
      </form>

      {run.state === "error" && (
        <p className="auth-error" role="alert">
          {run.message}
        </p>
      )}
      {run.state === "ready" && (
        <StrategyResult run={run.run} onStagePlan={onStagePlan} />
      )}
    </section>
  );
}

function StrategyResult({
  run,
  onStagePlan,
}: {
  run: StrategyRun;
  onStagePlan?: (plan: StrategyPlan) => void;
}) {
  const decision = run.decision;
  return (
    <div className="strategy-result">
      <p className="decision-preview">
        Super Agent source <strong>{decision.source}</strong>
        {run.spec.playbook !== null ? ` · playbook ${run.spec.playbook}` : ""}
        {decision.model !== null ? ` · ${decision.model}` : ""}
        {decision.fallback_used
          ? ` · fallback from ${decision.fallback_from ?? "unknown"}`
          : ""}
      </p>
      <p className="panel-note">{decision.strategy_summary}</p>

      <h3>Ranked actions</h3>
      <table className="judgment-table">
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">Symbol</th>
            <th scope="col">Action</th>
            <th scope="col">Size scale</th>
            <th scope="col">Rationale</th>
          </tr>
        </thead>
        <tbody>
          {decision.ranked_actions.map((action) => (
            <tr key={action.symbol}>
              <td>{action.rank}</td>
              <th scope="row">{action.symbol}</th>
              <td>{action.action}</td>
              <td>{action.size_scale.toFixed(2)}</td>
              <td>{action.rationale}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Accepted paper plans</h3>
      {run.accepted.length === 0 ? (
        <p className="panel-note">No plan passed the deterministic gate.</p>
      ) : (
        <table className="judgment-table">
          <thead>
            <tr>
              <th scope="col">Symbol</th>
              <th scope="col">Qty</th>
              <th scope="col">Entry</th>
              <th scope="col">Stop</th>
              <th scope="col">Target</th>
              <th scope="col">R:R</th>
              <th scope="col">Notional</th>
              <th scope="col">Scale</th>
              <th scope="col">Action</th>
            </tr>
          </thead>
          <tbody>
            {run.accepted.map((item) => (
              <tr key={item.symbol}>
                <th scope="row">{item.symbol}</th>
                <td>{item.plan.suggested_quantity}</td>
                <td>{item.plan.entry_price}</td>
                <td>{item.plan.stop_loss}</td>
                <td>{item.plan.target_price}</td>
                <td>{item.plan.reward_risk}</td>
                <td>{formatPaise(item.notional_paise)}</td>
                <td>{item.size_scale.toFixed(2)}</td>
                <td>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => onStagePlan?.(item.plan)}
                    disabled={onStagePlan === undefined}
                  >
                    Stage in Trade
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {run.rejected.length > 0 && (
        <>
          <h3>Rejected</h3>
          <ul className="task-list">
            {run.rejected.map((item) => (
              <li key={`${item.symbol}-${item.reason}`}>
                <strong>{item.symbol}</strong>
                <div>
                  {item.action}: {item.reason}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3>Portfolio notes</h3>
      <p className="panel-note">{decision.portfolio_notes}</p>
      <p className="panel-note">{decision.risk_notes}</p>

      <h3>Per-symbol agent runs</h3>
      <div className="strategy-symbols">
        {run.symbols.map((symbol) => (
          <SymbolCard key={symbol.symbol} symbol={symbol} />
        ))}
      </div>

      <h3>Run notes</h3>
      <ul className="task-list">
        {run.notes.map((note) => (
          <li key={note}>{note}</li>
        ))}
      </ul>
    </div>
  );
}

function SymbolCard({ symbol }: { symbol: StrategySymbol }) {
  if (!symbol.ok) {
    return (
      <article className="strategy-symbol strategy-symbol--failed">
        <div className="strategy-symbol-head">
          <strong>{symbol.symbol}</strong>
          <span className="status-badge status-badge--provisional">failed</span>
        </div>
        <p className="auth-error">{symbol.error}</p>
      </article>
    );
  }
  return (
    <article className="strategy-symbol">
      <div className="strategy-symbol-head">
        <strong>{symbol.symbol}</strong>
        <span
          className={`status-badge ${
            symbol.signal === "BUY"
              ? "status-badge--allow"
              : "status-badge--provisional"
          }`}
        >
          {symbol.signal}
        </span>
        <span className="prompt-meta">
          composite {symbol.composite === null ? "n/a" : symbol.composite.toFixed(2)}
          {" · "}
          {symbol.mode}
        </span>
      </div>

      {symbol.plan !== null && (
        <dl className="settings-facts">
          <div>
            <dt>Entry</dt>
            <dd>{symbol.plan.entry_price}</dd>
          </div>
          <div>
            <dt>Stop</dt>
            <dd>{symbol.plan.stop_loss}</dd>
          </div>
          <div>
            <dt>Target</dt>
            <dd>{symbol.plan.target_price}</dd>
          </div>
          <div>
            <dt>R:R</dt>
            <dd>{symbol.plan.reward_risk}</dd>
          </div>
          <div>
            <dt>Quantity</dt>
            <dd>{symbol.plan.suggested_quantity}</dd>
          </div>
        </dl>
      )}

      {symbol.agents.length > 0 && (
        <ol className="agent-flow" aria-label={`${symbol.symbol} agents`}>
          {symbol.agents.map((agent) => (
            <li key={agent.node} className="agent-flow-step">
              <span className={`trace-status trace-status--${agent.status}`}>
                {agent.status}
              </span>
              <strong>{agent.role}</strong>
              <span className="agent-flow-summary">{agent.summary}</span>
            </li>
          ))}
        </ol>
      )}

      {symbol.vetoes.length > 0 && (
        <p className="panel-note">Vetoes: {symbol.vetoes.join(", ")}</p>
      )}
      {symbol.facts.length > 0 && (
        <ul className="task-list">
          {symbol.facts.map((fact) => (
            <li key={fact}>{fact}</li>
          ))}
        </ul>
      )}
    </article>
  );
}
