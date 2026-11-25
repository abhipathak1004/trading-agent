import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { parseAgentRun, parseAgentStatus } from "./agentContract";
import type { AgentRun, AgentStatus } from "./agentContract";

type Status =
  | { state: "loading" | "error" }
  | { state: "ready"; status: AgentStatus };

type Turn =
  | { kind: "task"; text: string }
  | { kind: "answer"; run: AgentRun }
  | { kind: "failed"; message: string };

const SUGGESTIONS = [
  "Summarise today's long-term market news and flag risks for Indian equities.",
  "What happened with the RBI recently, and what does it mean for rate-sensitive sectors?",
  "Quote RELIANCE.NS and TCS.NS and compare their recent performance.",
  "Any regulatory or geopolitical news that could move markets this week?",
];

function tokenLine(run: AgentRun): string {
  const parts: string[] = [];
  if (run.input_tokens !== null || run.output_tokens !== null) {
    parts.push(
      `${run.input_tokens ?? "?"} in / ${run.output_tokens ?? "?"} out tokens`,
    );
  }
  parts.push(`${run.turns} turn${run.turns === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

export default function AgentConsole({ csrfToken }: { csrfToken: string }) {
  const [status, setStatus] = useState<Status>({ state: "loading" });
  const [retry, setRetry] = useState(0);
  const [task, setTask] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [running, setRunning] = useState(false);
  const [showTools, setShowTools] = useState(false);
  const endRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const response = await fetch("/api/v1/agent/status", {
        cache: "no-store",
        signal,
      });
      if (!response.ok) {
        throw new Error("Agent status unavailable");
      }
      setStatus({ state: "ready", status: parseAgentStatus(await response.json()) });
    } catch {
      if (!signal.aborted) {
        setStatus({ state: "error" });
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, retry]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, running]);

  async function ask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const question = task.trim();
    if (!question || running) {
      return;
    }
    setTask("");
    setTurns((current) => [...current, { kind: "task", text: question }]);
    setRunning(true);
    try {
      const response = await fetch("/api/v1/agent/run", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ task: question, max_turns: 10 }),
      });
      if (!response.ok) {
        let message = "The agent could not run.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setTurns((current) => [...current, { kind: "failed", message }]);
        return;
      }
      const run = parseAgentRun(await response.json());
      setTurns((current) => [...current, { kind: "answer", run }]);
    } catch {
      setTurns((current) => [
        ...current,
        { kind: "failed", message: "The agent request failed. Check the local service." },
      ]);
    } finally {
      setRunning(false);
    }
  }

  const ready = status.state === "ready";
  const agent = ready ? status.status : null;
  const configured = agent?.configured ?? false;

  return (
    <section className="agent-console" aria-labelledby="agent-title">
      <header className="agent-head">
        <div>
          <h2 id="agent-title">Research agent</h2>
          <p className="panel-note">
            {agent
              ? `${agent.provider} · ${agent.model || "no model"} · ${agent.tools.length} tools`
              : "Loading agent..."}
          </p>
        </div>
        {agent && (
          <button
            type="button"
            className="text-button"
            onClick={() => setShowTools((value) => !value)}
            aria-expanded={showTools}
          >
            {showTools ? "Hide tools" : "Show tools"}
          </button>
        )}
      </header>

      {status.state === "loading" && <p role="status">Connecting to the agent...</p>}
      {status.state === "error" && (
        <p role="alert">
          Agent status is unavailable.{" "}
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setStatus({ state: "loading" });
              setRetry((value) => value + 1);
            }}
          >
            Retry
          </button>
        </p>
      )}

      {agent && !configured && (
        <p className="agent-warning" role="alert">
          No model is configured. Add a provider key and model name in{" "}
          <strong>Settings</strong> to use the agent.
        </p>
      )}

      {showTools && agent && (
        <ul className="agent-tools">
          {agent.tools.map((tool) => (
            <li key={tool.name}>
              <strong>{tool.name}</strong>
              {tool.mutating && <span className="news-tag">writes</span>}
              <span>{tool.summary}</span>
            </li>
          ))}
        </ul>
      )}

      <div className="agent-thread" aria-live="polite">
        {turns.length === 0 && (
          <div className="agent-empty">
            <p>Ask the agent to research markets. It will call tools and cite what it finds.</p>
            <div className="agent-suggestions">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  className="agent-suggestion"
                  onClick={() => setTask(suggestion)}
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((turn, index) => {
          if (turn.kind === "task") {
            return (
              <div className="agent-msg agent-msg--user" key={index}>
                <span className="agent-msg__label">You</span>
                <p>{turn.text}</p>
              </div>
            );
          }
          if (turn.kind === "failed") {
            return (
              <div className="agent-msg agent-msg--error" key={index}>
                <span className="agent-msg__label">Error</span>
                <p>{turn.message}</p>
              </div>
            );
          }
          return <AnswerBubble run={turn.run} key={index} />;
        })}

        {running && (
          <div className="agent-msg agent-msg--agent">
            <span className="agent-msg__label">Agent</span>
            <p className="agent-working">
              <span className="agent-pulse" aria-hidden="true" />
              Researching — calling tools...
            </p>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form className="agent-input" onSubmit={(event) => void ask(event)}>
        <textarea
          value={task}
          onChange={(event) => setTask(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder={
            configured
              ? "Ask the research agent… (Enter to send, Shift+Enter for a new line)"
              : "Configure a model in Settings first"
          }
          rows={2}
          maxLength={4000}
          disabled={running || !configured}
        />
        <button
          type="submit"
          className="auth-button"
          disabled={running || !configured || task.trim().length === 0}
        >
          {running ? "Working..." : "Ask"}
        </button>
      </form>
    </section>
  );
}

function AnswerBubble({ run }: { run: AgentRun }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="agent-msg agent-msg--agent">
      <span className="agent-msg__label">Agent</span>
      <div className="agent-answer">{renderAnswer(run.final_text)}</div>
      <div className="agent-meta">
        <span>{tokenLine(run)}</span>
        {!run.stopped && <span className="agent-meta__warn">turn limit reached</span>}
        {run.steps.length > 0 && (
          <button
            type="button"
            className="text-button"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
          >
            {open ? "Hide steps" : `Steps (${run.steps.length})`}
          </button>
        )}
      </div>
      {open && (
        <ol className="agent-steps">
          {run.steps.map((step) => (
            <li key={`${step.turn}-${step.tool}`} className={`agent-step agent-step--${step.kind}`}>
              <div className="agent-step__head">
                <strong>{step.tool}</strong>
                <span>turn {step.turn}</span>
                {step.kind !== "tool" && <span className="news-tag">{step.kind}</span>}
              </div>
              {Object.keys(step.arguments).length > 0 && (
                <code>{JSON.stringify(step.arguments)}</code>
              )}
              <pre>{step.output}</pre>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function renderAnswer(text: string) {
  // Minimal, safe rendering: split on blank lines into paragraphs. No HTML.
  const paragraphs = text.split(/\n{2,}/).filter((block) => block.trim().length > 0);
  return paragraphs.map((block, index) => <p key={index}>{block}</p>);
}
