import { useState } from "react";
import type { FormEvent } from "react";

import { parseSemanticSearch } from "./semanticContract";
import type { SemanticSearch } from "./semanticContract";

type SearchState =
  | { state: "idle" | "running" }
  | { state: "error"; message: string }
  | { state: "ready"; result: SemanticSearch };

export default function SemanticMemoryPanel() {
  const [query, setQuery] = useState("breakout volume");
  const [symbol, setSymbol] = useState("");
  const [search, setSearch] = useState<SearchState>({ state: "idle" });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!query.trim()) {
      setSearch({ state: "error", message: "A query is required." });
      return;
    }
    setSearch({ state: "running" });
    const params = new URLSearchParams({ q: query, limit: "10" });
    if (symbol.trim()) {
      params.set("symbol", symbol.trim().toUpperCase());
    }
    try {
      const response = await fetch(`/api/v1/memory/semantic?${params}`, {
        cache: "no-store",
      });
      if (!response.ok) {
        let message = "Semantic search failed.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setSearch({ state: "error", message });
        return;
      }
      setSearch({ state: "ready", result: parseSemanticSearch(await response.json()) });
    } catch {
      setSearch({
        state: "error",
        message: "Semantic search failed. Check the local service.",
      });
    }
  }

  return (
    <section className="settings-panel" aria-labelledby="semantic-memory-title">
      <h2 id="semantic-memory-title">Semantic memory</h2>
      <p className="panel-note">
        Point-in-time search over stored research documents. Results are filtered
        to documents published, known and recorded at or before the request
        instant. FTS5 is used when available; otherwise a deterministic term
        index answers the same query.
      </p>
      <form className="config-form" onSubmit={(event) => void submit(event)}>
        <label htmlFor="semantic-query">Query</label>
        <input
          id="semantic-query"
          type="text"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          maxLength={256}
        />
        <label htmlFor="semantic-symbol">Symbol filter (optional)</label>
        <input
          id="semantic-symbol"
          type="text"
          value={symbol}
          onChange={(event) => setSymbol(event.target.value)}
          placeholder="NSE:RELIANCE"
        />
        <button
          type="submit"
          className="auth-button"
          disabled={search.state === "running"}
        >
          {search.state === "running" ? "Searching..." : "Search memory"}
        </button>
      </form>
      {search.state === "error" && (
        <p className="auth-error" role="alert">
          {search.message}
        </p>
      )}
      {search.state === "ready" &&
        (search.result.hits.length === 0 ? (
          <p className="panel-note">No document matched at or before now.</p>
        ) : (
          <ul className="task-list">
            {search.result.hits.map((hit) => (
              <li key={hit.doc_id}>
                <strong>
                  {hit.symbol} · {hit.title}
                </strong>
                <div>{hit.snippet}</div>
                <div className="prompt-meta">
                  {hit.published_at} · score {hit.score.toFixed(2)} ·{" "}
                  <a href={hit.source_url} target="_blank" rel="noreferrer">
                    source
                  </a>
                </div>
              </li>
            ))}
          </ul>
        ))}
    </section>
  );
}
