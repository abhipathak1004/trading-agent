import assert from "node:assert/strict";
import test from "node:test";

import {
  parseSemanticDocument,
  parseSemanticSearch,
} from "../src/semanticContract.ts";

const valid = {
  query: "breakout volume",
  as_of: "2026-11-26T12:00:00+00:00",
  hits: [
    {
      doc_id: "sd_1",
      symbol: "NSE:RELIANCE",
      source_url: "https://example.com/reliance",
      title: "Reliance breakout",
      snippet: "Reliance cleared resistance on strong volume.",
      published_at: "2026-11-25T10:00:00+00:00",
      known_at: "2026-11-25T10:00:00+00:00",
      score: 1.2,
    },
  ],
};

test("a valid semantic search is accepted", () => {
  const parsed = parseSemanticSearch(valid);
  assert.equal(parsed.hits.length, 1);
  assert.equal(parsed.hits[0].symbol, "NSE:RELIANCE");
  assert.equal(parsed.hits[0].score, 1.2);
});

test("malformed semantic searches are rejected", () => {
  for (const override of [
    { hits: "no" },
    { hits: [{ ...valid.hits[0], score: "high" }] },
    { hits: [{ ...valid.hits[0], doc_id: "" }] },
    { as_of: 1 },
  ]) {
    assert.throws(() => parseSemanticSearch({ ...valid, ...override }));
  }
});

test("a stored semantic document parses", () => {
  const parsed = parseSemanticDocument({
    doc_id: "sd_1",
    symbol: "NSE:RELIANCE",
    source_url: "https://example.com/reliance",
    title: "Reliance breakout",
    snippet: "Reliance cleared resistance on strong volume.",
    published_at: "2026-11-25T10:00:00+00:00",
    known_at: "2026-11-25T10:00:00+00:00",
    recorded_at: "2026-11-26T12:00:00+00:00",
    content_hash: "abc123",
  });
  assert.equal(parsed.doc_id, "sd_1");
  assert.equal(parsed.content_hash, "abc123");
});

test("malformed stored semantic documents are rejected", () => {
  const base = {
    doc_id: "sd_1",
    symbol: "NSE:RELIANCE",
    source_url: "https://example.com/reliance",
    title: "Reliance breakout",
    snippet: "text",
    published_at: "2026-11-25T10:00:00+00:00",
    known_at: "2026-11-25T10:00:00+00:00",
    recorded_at: "2026-11-26T12:00:00+00:00",
    content_hash: "abc123",
  };
  assert.throws(() => parseSemanticDocument({ ...base, doc_id: "" }));
  assert.throws(() => parseSemanticDocument({ ...base, content_hash: "" }));
  assert.throws(() => parseSemanticDocument({ ...base, snippet: 1 }));
});
