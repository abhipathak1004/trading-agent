import { useState } from "react";

import { parseWorkingMemory } from "./workingMemoryContract";
import type { WorkingMemory, WorkingMemoryResponse } from "./workingMemoryContract";

type State =
  | { state: "idle" | "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; response: WorkingMemoryResponse };

function recordValue(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function summary(memory: WorkingMemory) {
  const technical = recordValue(memory.state.technical);
  const finalGate = recordValue(memory.state.final_gate);
  const plan = recordValue(memory.state.plan);
  const routing = Array.isArray(memory.state.routing)
    ? memory.state.routing.length
    : 0;
  const specialists = Array.isArray(memory.state.specialists)
    ? memory.state.specialists.length
    : 0;
  return {
    mode: typeof memory.state.mode === "string" ? memory.state.mode : "n/a",
    composite:
      technical && typeof technical.composite === "number"
        ? technical.composite.toFixed(2)
        : "n/a",
    routing,
    specialists,
    gate:
      finalGate && typeof finalGate.reason === "string"
        ? finalGate.reason
        : "not run",
    entry: plan && typeof plan.entry_price === "string" ? plan.entry_price : "no plan",
  };
}

export default function WorkingMemoryPanel({ runId }: { runId: string }) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<State>({ state: "idle" });

  async function load() {
    if (state.state === "ready") {
      setOpen((value) => !value);
      return;
    }
    setOpen(true);
    setState({ state: "loading" });
    try {
      const response = await fetch(
        `/api/v1/analysis/runs/${encodeURIComponent(runId)}/working-memory`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        throw new Error("Working memory unavailable");
      }
      setState({ state: "ready", response: parseWorkingMemory(await response.json()) });
    } catch {
      setState({ state: "error", message: "Working memory is unavailable." });
    }
  }

  return (
    <div className="working-memory">
      <button
        type="button"
        className="text-button"
        aria-expanded={open}
        onClick={() => void load()}
      >
        {open ? "Hide working memory" : "Show working memory"}
      </button>
      {open && state.state === "loading" && (
        <p className="panel-note" role="status">
          Loading working memory...
        </p>
      )}
      {open && state.state === "error" && (
        <p className="auth-error" role="alert">
          {state.message}
        </p>
      )}
      {open && state.state === "ready" &&
        state.response.memories.map((memory) => {
          const info = summary(memory);
          return (
            <details key={`${memory.run_id}-${memory.symbol}`} className="working-memory-record">
              <summary>
                {memory.symbol} · {memory.source} · {info.mode} · composite{" "}
                {info.composite} · gate {info.gate}
              </summary>
              <dl className="settings-facts">
                <div>
                  <dt>As of</dt>
                  <dd>{memory.as_of}</dd>
                </div>
                <div>
                  <dt>Routing entries</dt>
                  <dd>{info.routing}</dd>
                </div>
                <div>
                  <dt>Specialists</dt>
                  <dd>{info.specialists}</dd>
                </div>
                <div>
                  <dt>Plan entry</dt>
                  <dd>{info.entry}</dd>
                </div>
                <div>
                  <dt>State hash</dt>
                  <dd>{memory.state_hash}</dd>
                </div>
              </dl>
              <pre className="working-memory-json">
                {JSON.stringify(memory.state, null, 2)}
              </pre>
            </details>
          );
        })}
    </div>
  );
}
