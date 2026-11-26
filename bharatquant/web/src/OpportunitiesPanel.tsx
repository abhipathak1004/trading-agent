import { useEffect, useState } from "react";

import {
  parseOpportunity,
  parseOpportunityList,
  parseShadowSummary,
} from "./opportunityContract.ts";
import type { Opportunity, ShadowSummary } from "./opportunityContract.ts";
import type { StrategyPlan } from "./strategyContract.ts";

type LoadState =
  | { state: "loading" | "error" }
  | { state: "ready"; opportunities: Opportunity[] };

type ActionState = "idle" | "saving" | "error";

const LIFECYCLE_ACTIONS = ["WATCHING", "DISMISSED"] as const;

function levelClass(level: string): string {
  if (level === "VERY_HIGH" || level === "HIGH") {
    return "status-badge status-badge--allow";
  }
  if (level === "MODERATE") {
    return "status-badge status-badge--provisional";
  }
  return "status-badge status-badge--blocked";
}

export default function OpportunitiesPanel({
  csrfToken,
  onStagePlan,
}: {
  csrfToken: string;
  onStagePlan?: (plan: StrategyPlan) => void;
}) {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [summary, setSummary] = useState<ShadowSummary | null>(null);
  const [levelFilter, setLevelFilter] = useState("ALL");
  const [action, setAction] = useState<ActionState>("idle");
  const [message, setMessage] = useState("");
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function loadBoard() {
      try {
        const [boardResponse, summaryResponse] = await Promise.all([
          fetch("/api/v1/opportunities?limit=50", {
            cache: "no-store",
            signal: controller.signal,
          }),
          fetch("/api/v1/opportunities/shadow/summary", {
            cache: "no-store",
            signal: controller.signal,
          }),
        ]);
        if (!boardResponse.ok) {
          throw new Error("Opportunities unavailable");
        }
        setLoad({
          state: "ready",
          opportunities: parseOpportunityList(await boardResponse.json())
            .opportunities,
        });
        if (summaryResponse.ok) {
          setSummary(parseShadowSummary(await summaryResponse.json()));
        }
      } catch {
        if (!controller.signal.aborted) {
          setLoad({ state: "error" });
        }
      }
    }
    void loadBoard();
    return () => controller.abort();
  }, [version]);

  async function transition(opportunity: Opportunity, lifecycle: string) {
    setAction("saving");
    setMessage("");
    try {
      const response = await fetch(
        `/api/v1/opportunities/${encodeURIComponent(opportunity.opportunity_id)}/transition`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({ lifecycle }),
        },
      );
      if (!response.ok) {
        let messageText = "The opportunity could not be updated.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            messageText = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setAction("error");
        setMessage(messageText);
        return;
      }
      parseOpportunity(await response.json());
      setAction("idle");
      setMessage(`${opportunity.instrument_id} moved to ${lifecycle}.`);
      setVersion((value) => value + 1);
    } catch {
      setAction("error");
      setMessage("The opportunity could not be updated.");
    }
  }

  async function resolveShadows() {
    setAction("saving");
    setMessage("");
    try {
      const response = await fetch("/api/v1/opportunities/shadow/resolve", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (!response.ok) {
        throw new Error("Shadow resolution failed");
      }
      const report = (await response.json()) as {
        resolved?: unknown;
        examined?: unknown;
      };
      const resolved = typeof report.resolved === "number" ? report.resolved : 0;
      const examined = typeof report.examined === "number" ? report.examined : 0;
      setAction("idle");
      setMessage(`Resolved ${resolved} outcome(s) across ${examined} shadow trade(s).`);
      setVersion((value) => value + 1);
    } catch {
      setAction("error");
      setMessage("Shadow resolution failed.");
    }
  }

  const opportunities =
    load.state === "ready"
      ? load.opportunities.filter(
          (item) => levelFilter === "ALL" || item.level === levelFilter,
        )
      : [];

  return (
    <section className="settings-panel" aria-labelledby="opportunities-title">
      <h2 id="opportunities-title">Opportunity board</h2>
      <p className="panel-note">
        Confidence decides whether a candidate is suggested; the score only ranks
        it. Every candidate is shadow-tracked, including the ones you dismiss. The
        board never places an order by itself.
      </p>

      {summary !== null && (
        <p className="decision-preview">
          Shadow filter: <strong>{summary.verdict}</strong> ·{" "}
          {summary.suggested_count} suggested / {summary.unsuggested_count} other
          resolved · suggested win rate{" "}
          {summary.suggested_win_rate === null
            ? "n/a"
            : `${(summary.suggested_win_rate * 100).toFixed(1)}%`}{" "}
          <button
            type="button"
            className="text-button"
            onClick={() => void resolveShadows()}
            disabled={action === "saving"}
          >
            Resolve shadow outcomes
          </button>
        </p>
      )}

      <label htmlFor="opportunity-level">
        Level
        <select
          id="opportunity-level"
          value={levelFilter}
          onChange={(event) => setLevelFilter(event.target.value)}
        >
          <option value="ALL">All</option>
          <option value="VERY_HIGH">VERY HIGH</option>
          <option value="HIGH">HIGH</option>
          <option value="MODERATE">MODERATE</option>
          <option value="LOW">LOW</option>
        </select>
      </label>

      {load.state === "loading" && <p role="status">Loading opportunities...</p>}
      {load.state === "error" && (
        <p role="alert">
          Opportunities are unavailable.{" "}
          <button
            type="button"
            className="text-button"
            onClick={() => setVersion((value) => value + 1)}
          >
            Retry
          </button>
        </p>
      )}
      {message !== "" && (
        <p className={action === "error" ? "auth-error" : "panel-note"} role="status">
          {message}
        </p>
      )}

      {load.state === "ready" && opportunities.length === 0 && (
        <p className="panel-note">No opportunities match this filter yet.</p>
      )}

      {opportunities.length > 0 && (
        <table className="judgment-table">
          <thead>
            <tr>
              <th scope="col">Symbol</th>
              <th scope="col">Score</th>
              <th scope="col">Level</th>
              <th scope="col">Conf.</th>
              <th scope="col">Horizon</th>
              <th scope="col">Lifecycle</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {opportunities.map((item) => (
              <tr key={item.opportunity_id}>
                <th scope="row">{item.instrument_id}</th>
                <td>{item.score.toFixed(1)}</td>
                <td><span className={levelClass(item.level)}>{item.level}</span></td>
                <td>{(item.probability * 100).toFixed(0)}%</td>
                <td>{item.horizon}</td>
                <td>{item.lifecycle}</td>
                <td>
                  {LIFECYCLE_ACTIONS.map((target) => (
                    <button
                      key={target}
                      type="button"
                      className="text-button"
                      disabled={action === "saving" || item.lifecycle === target}
                      onClick={() => void transition(item, target)}
                    >
                      {target === "WATCHING" ? "Watch" : "Dismiss"}
                    </button>
                  ))}
                  {item.plan !== null && onStagePlan !== undefined && (
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => onStagePlan(item.plan as StrategyPlan)}
                    >
                      Stage in Trade
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {opportunities.length > 0 && (
        <details className="working-memory">
          <summary>Evidence and plan detail</summary>
          {opportunities.map((item) => (
            <article key={item.opportunity_id} className="specialist-brief">
              <div className="specialist-brief-head">
                <strong>{item.instrument_id}</strong>
                <span className="prompt-meta">
                  {item.setup_id ?? "no setup"} · EV{" "}
                  {item.ev_r_after_costs.toFixed(2)}R · base n{" "}
                  {item.base_rate_samples}
                </span>
              </div>
              {item.confidence !== null && (
                <ul className="agent-tools">
                  {item.confidence.evidence.map((evidence) => (
                    <li key={evidence.family}>
                      {evidence.family}: {evidence.state} ({evidence.strength.toFixed(2)})
                    </li>
                  ))}
                  {item.confidence.vetoes.length > 0 && (
                    <li>Vetoes: {item.confidence.vetoes.join(", ")}</li>
                  )}
                </ul>
              )}
              {item.plan !== null && (
                <p className="prompt-meta">
                  Entry {item.plan.entry_price} · Stop {item.plan.stop_loss} · Target{" "}
                  {item.plan.target_price} · R:R {item.plan.reward_risk}
                </p>
              )}
            </article>
          ))}
        </details>
      )}
    </section>
  );
}
