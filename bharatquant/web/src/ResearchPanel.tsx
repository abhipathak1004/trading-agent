import { useCallback, useEffect, useState } from "react";
import { formatUtc } from "./contracts";

import { parseDigest, parseIngestResult } from "./newsContract";
import type { NewsDigest, NewsIngestResult } from "./newsContract";

type Status =
  | { state: "loading" | "error" }
  | { state: "ready"; digest: NewsDigest };

type RunState =
  | { state: "idle" }
  | { state: "running" }
  | { state: "done"; result: NewsIngestResult }
  | { state: "failed"; message: string };

export default function ResearchPanel({ csrfToken }: { csrfToken: string }) {
  const [status, setStatus] = useState<Status>({ state: "loading" });
  const [retry, setRetry] = useState(0);
  const [run, setRun] = useState<RunState>({ state: "idle" });

  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const response = await fetch("/api/v1/news/digest", {
        cache: "no-store",
        signal,
      });
      if (!response.ok) {
        throw new Error("Research digest unavailable");
      }
      const value: unknown = await response.json();
      setStatus({ state: "ready", digest: parseDigest(value) });
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

  async function ingest() {
    setRun({ state: "running" });
    try {
      const response = await fetch("/api/v1/news/ingest", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (!response.ok) {
        let message = "Ingest failed.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic failure message.
        }
        setRun({ state: "failed", message });
        return;
      }
      const value: unknown = await response.json();
      setRun({ state: "done", result: parseIngestResult(value) });
      setStatus({ state: "loading" });
      setRetry((current) => current + 1);
    } catch {
      setRun({ state: "failed", message: "Ingest failed. Check the local service." });
    }
  }

  const running = run.state === "running";

  return (
    <section className="data-panel research-panel" aria-labelledby="research-title">
      <header className="panel-header">
        <h2 id="research-title">News feed</h2>
        <button
          type="button"
          className="auth-button auth-button--compact"
          onClick={() => void ingest()}
          disabled={running}
        >
          {running ? "Ingesting..." : "Ingest now"}
        </button>
      </header>

      <p className="panel-note">
        Deterministic ingest from the configured feed. No model calls; full
        articles are never fetched. These are the stories the agent can search.
      </p>

      {run.state === "done" && (
        <p className="research-result" role="status">
          Fetched {run.result.fetched.toLocaleString("en-IN")} · stored{" "}
          {run.result.stored.toLocaleString("en-IN")} · duplicates{" "}
          {run.result.duplicates.toLocaleString("en-IN")} · new long-term
          memories {run.result.long_term_memories.toLocaleString("en-IN")}.
        </p>
      )}
      {run.state === "failed" && (
        <p className="auth-error" role="alert">
          {run.message}
        </p>
      )}

      {status.state === "loading" && <p role="status">Loading research digest...</p>}
      {status.state === "error" && (
        <p role="alert">
          Research digest is unavailable.{" "}
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
      {status.state === "ready" && (
        <>
          <p className="research-counts">
            {status.digest.item_count.toLocaleString("en-IN")} recent stories ·{" "}
            {status.digest.memory_count.toLocaleString("en-IN")} long-term memories
          </p>

          <h3>Long-term memory (up to 20)</h3>
          {status.digest.memories.length === 0 ? (
            <p>
              No long-term memories yet. Run an ingest, or wait for structural
              news (policy, regulation, geopolitics) to arrive.
            </p>
          ) : (
            <ul className="news-list">
              {status.digest.memories.map((memory) => (
                <li key={memory.memory_id}>
                  <span className="news-tag">{memory.category}</span>
                  <strong>{memory.title}</strong>
                  {memory.body && <span>{memory.body}</span>}
                  <time dateTime={memory.known_at}>
                    {formatUtc(memory.known_at)}
                  </time>
                </li>
              ))}
            </ul>
          )}

          <h3>Recent stories (up to 20)</h3>
          {status.digest.items.length === 0 ? (
            <p>No stories ingested yet.</p>
          ) : (
            <ul className="news-list">
              {status.digest.items.map((item) => (
                <li key={item.news_id}>
                  <span className="news-tag">{item.category}</span>
                  <a href={item.url} rel="noreferrer noopener" target="_blank">
                    {item.title}
                  </a>
                  <time dateTime={item.published_at}>
                    {formatUtc(item.published_at)}
                  </time>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
