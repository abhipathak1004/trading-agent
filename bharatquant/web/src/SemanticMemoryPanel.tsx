import { useState } from "react";
import type { FormEvent } from "react";

import {
  parseSemanticDocument,
  parseSemanticSearch,
} from "./semanticContract";
import type { SemanticDocument, SemanticSearch } from "./semanticContract";

type SearchState =
  | { state: "idle" | "running" }
  | { state: "error"; message: string }
  | { state: "ready"; result: SemanticSearch };

type IngestState =
  | { state: "idle" | "saving" }
  | { state: "error"; message: string }
  | { state: "ready"; document: SemanticDocument };

function localDateTime(value: Date): string {
  const shifted = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
}

export default function SemanticMemoryPanel({
  csrfToken,
}: {
  csrfToken: string;
}) {
  const [query, setQuery] = useState("breakout volume");
  const [symbol, setSymbol] = useState("");
  const [search, setSearch] = useState<SearchState>({ state: "idle" });
  const [ingestSymbol, setIngestSymbol] = useState("NSE:RELIANCE");
  const [sourceUrl, setSourceUrl] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [publishedAt, setPublishedAt] = useState(localDateTime(new Date()));
  const [knownAt, setKnownAt] = useState("");
  const [ingest, setIngest] = useState<IngestState>({ state: "idle" });

  async function runSearch() {
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
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            message = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setSearch({ state: "error", message });
        return;
      }
      setSearch({
        state: "ready",
        result: parseSemanticSearch(await response.json()),
      });
    } catch {
      setSearch({
        state: "error",
        message: "Semantic search failed. Check the local service.",
      });
    }
  }

  async function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await runSearch();
  }

  async function submitIngest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !ingestSymbol.trim() ||
      !sourceUrl.trim() ||
      !title.trim() ||
      !body.trim() ||
      !publishedAt
    ) {
      setIngest({
        state: "error",
        message: "Symbol, HTTPS source, title, body and published time are required.",
      });
      return;
    }
    const published = new Date(publishedAt);
    if (!Number.isFinite(published.getTime())) {
      setIngest({ state: "error", message: "Published time is invalid." });
      return;
    }
    let known: string | null = null;
    if (knownAt.trim()) {
      const parsed = new Date(knownAt);
      if (!Number.isFinite(parsed.getTime())) {
        setIngest({ state: "error", message: "Known time is invalid." });
        return;
      }
      known = parsed.toISOString();
    }
    setIngest({ state: "saving" });
    try {
      const response = await fetch("/api/v1/memory/semantic", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          symbol: ingestSymbol.trim().toUpperCase(),
          source_url: sourceUrl.trim(),
          title: title.trim(),
          body: body.trim(),
          published_at: published.toISOString(),
          known_at: known,
        }),
      });
      if (!response.ok) {
        let message = "The document could not be stored.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            message = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setIngest({ state: "error", message });
        return;
      }
      const document = parseSemanticDocument(await response.json());
      setIngest({ state: "ready", document });
      setSourceUrl("");
      setTitle("");
      setBody("");
      setKnownAt("");
      if (query.trim()) {
        await runSearch();
      }
    } catch {
      setIngest({
        state: "error",
        message: "The document could not be stored. Check the local service.",
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

      <form className="config-form" onSubmit={(event) => void submitSearch(event)}>
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

      <h3>Add research document</h3>
      <p className="panel-note">
        The owner supplies the document text. No web fetch and no model call are
        made. The source must be HTTPS without embedded credentials.
      </p>
      <form className="config-form" onSubmit={(event) => void submitIngest(event)}>
        <div className="models-grid models-grid--compact">
          <label htmlFor="semantic-ingest-symbol">
            Symbol
            <input
              id="semantic-ingest-symbol"
              type="text"
              maxLength={64}
              value={ingestSymbol}
              onChange={(event) => setIngestSymbol(event.target.value)}
            />
          </label>
          <label htmlFor="semantic-ingest-url">
            HTTPS source
            <input
              id="semantic-ingest-url"
              type="url"
              maxLength={2048}
              value={sourceUrl}
              onChange={(event) => setSourceUrl(event.target.value)}
              placeholder="https://example.com/research"
            />
          </label>
          <label htmlFor="semantic-ingest-published">
            Published at
            <input
              id="semantic-ingest-published"
              type="datetime-local"
              value={publishedAt}
              onChange={(event) => setPublishedAt(event.target.value)}
            />
          </label>
          <label htmlFor="semantic-ingest-known">
            Known at (optional; default now)
            <input
              id="semantic-ingest-known"
              type="datetime-local"
              value={knownAt}
              onChange={(event) => setKnownAt(event.target.value)}
            />
          </label>
        </div>
        <label htmlFor="semantic-ingest-title">Title</label>
        <input
          id="semantic-ingest-title"
          type="text"
          maxLength={512}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
        />
        <label htmlFor="semantic-ingest-body">Body</label>
        <textarea
          id="semantic-ingest-body"
          rows={6}
          maxLength={16_384}
          value={body}
          onChange={(event) => setBody(event.target.value)}
        />
        <button
          type="submit"
          className="auth-button"
          disabled={ingest.state === "saving"}
        >
          {ingest.state === "saving" ? "Storing..." : "Store document"}
        </button>
      </form>

      {ingest.state === "error" && (
        <p className="auth-error" role="alert">
          {ingest.message}
        </p>
      )}
      {ingest.state === "ready" && (
        <p className="panel-note" role="status">
          Stored <strong>{ingest.document.title}</strong> for{" "}
          {ingest.document.symbol} ({ingest.document.doc_id}).
        </p>
      )}
    </section>
  );
}
