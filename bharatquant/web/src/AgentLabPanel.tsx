import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import TableScroll from "./TableScroll";

import { parseAgentCatalogue } from "./agentCatalogueContract";
import type { AgentCatalogueItem } from "./agentCatalogueContract";
import { parseDecisionPreview } from "./decisionContract";
import type { DecisionPreview } from "./decisionContract";
import { parsePrompt, parsePromptList } from "./promptsContract";
import type { AgentPrompt } from "./promptsContract";

type DecisionState =
  | { state: "idle" | "running" }
  | { state: "error"; message: string }
  | { state: "ready"; result: DecisionPreview };

type PromptLoad =
  | { state: "loading" | "error" }
  | { state: "ready"; prompts: AgentPrompt[] };

type SaveState =
  | { state: "idle" | "saving" }
  | { state: "saved"; message: string }
  | { state: "failed"; message: string };

type CatalogueLoad =
  | { state: "loading" | "error" }
  | { state: "ready"; agents: AgentCatalogueItem[] };

function formatValue(value: string | number): string {
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(4);
  }
  return value;
}

async function errorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // Keep the fallback.
  }
  return fallback;
}

export default function AgentLabPanel({ csrfToken }: { csrfToken: string }) {
  const [symbol, setSymbol] = useState("NSE:RELIANCE");
  const [timeframe, setTimeframe] = useState<"swing" | "intraday">("swing");
  const [decision, setDecision] = useState<DecisionState>({ state: "idle" });

  const [promptLoad, setPromptLoad] = useState<PromptLoad>({ state: "loading" });
  const [promptRetry, setPromptRetry] = useState(0);
  const [agentId, setAgentId] = useState("bull_debater");
  const [system, setSystem] = useState("");
  const [template, setTemplate] = useState("");
  const [promptSave, setPromptSave] = useState<SaveState>({ state: "idle" });
  const [catalogue, setCatalogue] = useState<CatalogueLoad>({ state: "loading" });
  const [catalogueRetry, setCatalogueRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function loadPrompts() {
      try {
        const response = await fetch("/api/v1/prompts", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Prompts unavailable");
        }
        const prompts = parsePromptList(await response.json());
        setPromptLoad({ state: "ready", prompts });
        const selected =
          prompts.find((prompt) => prompt.agent_id === agentId) ?? prompts[0];
        if (selected !== undefined) {
          setAgentId(selected.agent_id);
          setSystem(selected.system);
          setTemplate(selected.template);
        }
      } catch {
        if (!controller.signal.aborted) {
          setPromptLoad({ state: "error" });
        }
      }
    }
    void loadPrompts();
    return () => controller.abort();
    // agentId is intentionally read once per load; selection is user-driven.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [promptRetry]);

  useEffect(() => {
    const controller = new AbortController();
    async function loadCatalogue() {
      try {
        const response = await fetch("/api/v1/agents", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Agent catalogue unavailable");
        }
        setCatalogue({ state: "ready", agents: parseAgentCatalogue(await response.json()) });
      } catch {
        if (!controller.signal.aborted) {
          setCatalogue({ state: "error" });
        }
      }
    }
    void loadCatalogue();
    return () => controller.abort();
  }, [catalogueRetry]);

  const prompts = promptLoad.state === "ready" ? promptLoad.prompts : [];
  const selected = prompts.find((prompt) => prompt.agent_id === agentId) ?? null;

  function selectAgent(next: string) {
    const prompt = prompts.find((item) => item.agent_id === next);
    setAgentId(next);
    if (prompt !== undefined) {
      setSystem(prompt.system);
      setTemplate(prompt.template);
    }
    setPromptSave({ state: "idle" });
  }

  function replacePrompt(prompt: AgentPrompt) {
    if (promptLoad.state !== "ready") {
      return;
    }
    const next = promptLoad.prompts.map((item) =>
      item.agent_id === prompt.agent_id ? prompt : item,
    );
    setPromptLoad({ state: "ready", prompts: next });
    setSystem(prompt.system);
    setTemplate(prompt.template);
  }

  async function runDecision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setDecision({ state: "running" });
    try {
      const response = await fetch("/api/v1/decision/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ symbol, timeframe }),
      });
      if (!response.ok) {
        setDecision({
          state: "error",
          message: await errorMessage(response, "The decision layer failed."),
        });
        return;
      }
      setDecision({ state: "ready", result: parseDecisionPreview(await response.json()) });
    } catch {
      setDecision({
        state: "error",
        message: "The decision layer failed. Check the local service.",
      });
    }
  }

  async function savePrompt(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (selected === null) {
      return;
    }
    setPromptSave({ state: "saving" });
    try {
      const response = await fetch(
        `/api/v1/prompts/${encodeURIComponent(selected.agent_id)}`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({ system, template }),
        },
      );
      if (!response.ok) {
        setPromptSave({
          state: "failed",
          message: await errorMessage(response, "Saving the prompt failed."),
        });
        return;
      }
      const saved = parsePrompt(await response.json());
      replacePrompt(saved);
      setPromptSave({
        state: "saved",
        message: `Saved version ${saved.version}.`,
      });
    } catch {
      setPromptSave({
        state: "failed",
        message: "Saving the prompt failed. Check the local service.",
      });
    }
  }

  async function resetPrompt() {
    if (selected === null) {
      return;
    }
    setPromptSave({ state: "saving" });
    try {
      const response = await fetch(
        `/api/v1/prompts/${encodeURIComponent(selected.agent_id)}/reset`,
        { method: "POST", headers: { "X-CSRF-Token": csrfToken } },
      );
      if (!response.ok) {
        setPromptSave({
          state: "failed",
          message: await errorMessage(response, "Resetting the prompt failed."),
        });
        return;
      }
      const reset = parsePrompt(await response.json());
      replacePrompt(reset);
      setPromptSave({
        state: "saved",
        message: `Reset saved as version ${reset.version}.`,
      });
    } catch {
      setPromptSave({
        state: "failed",
        message: "Resetting the prompt failed. Check the local service.",
      });
    }
  }

  return (
    <>
      <section className="settings-panel" aria-labelledby="decision-agent-title">
        <h2 id="decision-agent-title">Decision agent</h2>
        <p className="panel-note">
          Runs the Stage 0 screener battery on live cached data: deterministic state
          first, then Jev when configured, otherwise the configured fallback. No
          order is placed.
        </p>
        <form className="config-form" onSubmit={(event) => void runDecision(event)}>
          <label htmlFor="decision-symbol">Symbol</label>
          <input
            id="decision-symbol"
            type="text"
            value={symbol}
            onChange={(event) => setSymbol(event.target.value)}
            placeholder="NSE:RELIANCE"
          />
          <label htmlFor="decision-timeframe">Timeframe</label>
          <select
            id="decision-timeframe"
            value={timeframe}
            onChange={(event) =>
              setTimeframe(event.target.value as "swing" | "intraday")
            }
          >
            <option value="swing">swing</option>
            <option value="intraday">intraday</option>
          </select>
          <button
            type="submit"
            className="auth-button"
            disabled={decision.state === "running"}
          >
            {decision.state === "running" ? "Running..." : "Run decision agent"}
          </button>
        </form>

        {decision.state === "error" && (
          <p className="auth-error" role="alert">
            {decision.message}
          </p>
        )}

        {decision.state === "ready" && (
          <DecisionResult result={decision.result} />
        )}
      </section>

      <section className="settings-panel" aria-labelledby="prompt-editor-title">
        <h2 id="prompt-editor-title">Agent prompts</h2>
        <p className="panel-note">
          Refine the prompts without editing code. Every save appends a new
          version; the JSON contract and numeric rules still apply.
        </p>

        {promptLoad.state === "loading" && <p role="status">Loading prompts...</p>}
        {promptLoad.state === "error" && (
          <p role="alert">
            Prompts are unavailable.{" "}
            <button
              type="button"
              className="text-button"
              onClick={() => {
                setPromptLoad({ state: "loading" });
                setPromptRetry((value) => value + 1);
              }}
            >
              Retry
            </button>
          </p>
        )}

        {promptLoad.state === "ready" && selected !== null && (
          <form className="config-form" onSubmit={(event) => void savePrompt(event)}>
            <label htmlFor="prompt-agent">Agent</label>
            <select
              id="prompt-agent"
              value={agentId}
              onChange={(event) => selectAgent(event.target.value)}
            >
              {prompts.map((prompt) => (
                <option key={prompt.agent_id} value={prompt.agent_id}>
                  {prompt.label} · {prompt.agent_id}
                </option>
              ))}
            </select>

            <p className="panel-note">{selected.description}</p>
            <p className="prompt-meta">
              version {selected.version}
              {selected.is_default ? " · default" : " · customized"}
              {selected.updated_at !== null
                ? ` · ${new Date(selected.updated_at).toLocaleString()}`
                : " · never saved"}
            </p>

            <label htmlFor="prompt-system">System prompt</label>
            <textarea
              id="prompt-system"
              rows={8}
              value={system}
              onChange={(event) => setSystem(event.target.value)}
            />

            {selected.required_fields.length > 0 ? (
              <>
                <label htmlFor="prompt-template">User template</label>
                <textarea
                  id="prompt-template"
                  rows={10}
                  value={template}
                  onChange={(event) => setTemplate(event.target.value)}
                />
                <p className="panel-note">
                  Required placeholders:{" "}
                  {selected.required_fields.map((field) => (
                    <code key={field} className="prompt-field">
                      {"{"}
                      {field}
                      {"}"}
                    </code>
                  ))}
                </p>
              </>
            ) : (
              <p className="panel-note">
                This agent has no user template; the question set and answer
                types are code-owned.
              </p>
            )}

            <div className="prompt-actions">
              <button
                type="submit"
                className="auth-button"
                disabled={promptSave.state === "saving" || !system.trim()}
              >
                {promptSave.state === "saving" ? "Saving..." : "Save prompt version"}
              </button>
              <button
                type="button"
                className="text-button"
                onClick={() => void resetPrompt()}
                disabled={promptSave.state === "saving"}
              >
                Reset to default
              </button>
            </div>
          </form>
        )}

        {promptSave.state === "saved" && (
          <p className="config-feedback" role="status">
            {promptSave.message}
          </p>
        )}
        {promptSave.state === "failed" && (
          <p className="auth-error" role="alert">
            {promptSave.message}
          </p>
        )}
      </section>

      <section className="settings-panel" aria-labelledby="agent-catalogue-title">
        <h2 id="agent-catalogue-title">Agent catalogue</h2>
        <p className="panel-note">
          One entry per agent, with its resolved model route. The prompt editor
          above edits the editable agents; decision batteries are code-owned.
        </p>

        {catalogue.state === "loading" && <p role="status">Loading agents...</p>}
        {catalogue.state === "error" && (
          <p role="alert">
            Agent catalogue is unavailable.{" "}
            <button
              type="button"
              className="text-button"
              onClick={() => {
                setCatalogue({ state: "loading" });
                setCatalogueRetry((value) => value + 1);
              }}
            >
              Retry
            </button>
          </p>
        )}

        {catalogue.state === "ready" && (
          <TableScroll label="Agent catalogue">
            <table className="judgment-table">
              <thead>
                <tr>
                  <th scope="col">Agent</th>
                  <th scope="col">Kind</th>
                  <th scope="col">Default tier</th>
                  <th scope="col">Resolved route</th>
                  <th scope="col">Prompt</th>
                  <th scope="col">Tools</th>
                </tr>
              </thead>
              <tbody>
                {catalogue.agents.map((agent) => (
                  <tr key={agent.agent_id}>
                    <th scope="row">
                      <div>{agent.label}</div>
                      <div>{agent.agent_id}</div>
                    </th>
                    <td>{agent.kind}</td>
                    <td>{agent.tier}</td>
                    <td>
                      {agent.route.configured
                        ? `${agent.route.provider ?? "?"} · ${agent.route.model ?? "?"}`
                        : `${agent.route.tier} (not configured)`}
                    </td>
                    <td>{agent.prompt_id ?? "—"}</td>
                    <td>{agent.tools.length > 0 ? agent.tools.join(", ") : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        )}
      </section>
    </>
  );
}

function DecisionResult({ result }: { result: DecisionPreview }) {
  return (
    <div className="decision-result">
      <p className="decision-preview">
        {result.symbol} · {result.timeframe} · data {result.data_provenance.data_freshness}
        {" · "}
        decision source <strong>{result.route.decision_source}</strong> via{" "}
        <strong>{result.route.model}</strong>
        {result.route.fallback_used
          ? ` (fallback from ${result.route.fallback_from ?? "unknown"})`
          : ""}
      </p>

      <p
        className={`status-badge ${
          result.screener.verdict === "research"
            ? "status-badge--allow"
            : "status-badge--blocked"
        }`}
      >
        {result.screener.verdict}
      </p>
      <p className="panel-note">{result.screener.reason}</p>

      <h3>Battery judgments</h3>
      <TableScroll label="Battery judgments">
        <table className="judgment-table">
          <thead>
            <tr>
              <th scope="col">Question</th>
              <th scope="col">Answer</th>
              <th scope="col">Confidence</th>
            </tr>
          </thead>
          <tbody>
            {result.judgments.map((judgment) => (
              <tr key={judgment.name}>
                <th scope="row">{judgment.name}</th>
                <td>
                  {judgment.kind === "choice"
                    ? judgment.choice
                    : judgment.kind === "score"
                      ? judgment.score
                      : judgment.noul !== null
                        ? judgment.noul.toFixed(3)
                        : "n/a"}
                </td>
                <td>
                  {judgment.confidence === null
                    ? judgment.kind === "noul"
                      ? "n/a (noul)"
                      : "n/a"
                    : judgment.confidence.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>

      <h3>Route</h3>
      <dl className="settings-facts">
        <div>
          <dt>Tier</dt>
          <dd>{result.route.tier}</dd>
        </div>
        <div>
          <dt>Provider</dt>
          <dd>{result.route.provider}</dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd>{result.route.model}</dd>
        </div>
        <div>
          <dt>Latency</dt>
          <dd>
            {result.route.latency_ms === null
              ? "n/a"
              : `${result.route.latency_ms.toFixed(1)} ms`}
          </dd>
        </div>
        <div>
          <dt>Model mismatch</dt>
          <dd>{result.route.model_mismatch ? "yes" : "no"}</dd>
        </div>
        <div>
          <dt>State hash</dt>
          <dd>{result.route.state_hash ?? "n/a"}</dd>
        </div>
      </dl>

      <h3>Decision state</h3>
      <dl className="settings-facts">
        {Object.entries(result.state).map(([key, value]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>{formatValue(value)}</dd>
          </div>
        ))}
      </dl>

      {result.notes.length > 0 && (
        <>
          <h3>Notes</h3>
          <ul className="task-list">
            {result.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
