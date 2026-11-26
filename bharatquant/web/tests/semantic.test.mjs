import assert from "node:assert/strict";
import test from "node:test";

import { parseSemanticSearch } from "../src/semanticContract.ts";

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
