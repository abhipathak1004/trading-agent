import assert from "node:assert/strict";
import test from "node:test";

import { parseDigest, parseIngestResult } from "../src/newsContract.ts";

const item = {
  news_id: "n1",
  source_id: "pulse.zerodha",
  url: "https://example.com/story",
  title: "RBI holds repo rate",
  summary: "The committee kept the policy rate unchanged.",
  category: "RATES",
  long_term_impact: true,
  published_at: "2026-11-25T20:00:00Z",
};

const memory = {
  memory_id: "m1",
  category: "RATES",
  title: "RBI holds repo rate",
  body: "The committee kept the policy rate unchanged.",
  known_at: "2026-11-25T20:00:00Z",
};

test("a valid digest is accepted", () => {
  const parsed = parseDigest({
    source_id: "pulse.zerodha",
    item_count: 1,
    items: [item],
    memory_count: 1,
    memories: [memory],
  });
  assert.equal(parsed.items[0].category, "RATES");
  assert.equal(parsed.memories[0].memory_id, "m1");
});

test("digest rejects overreported counts and bad items", () => {
  assert.throws(() =>
    parseDigest({
      source_id: "pulse.zerodha",
      item_count: 0,
      items: [item],
      memory_count: 0,
      memories: [],
    }),
  );
  for (const bad of [
    { ...item, url: "http://example.com/story" },
    { ...item, category: "SPORTS" },
    { ...item, title: "" },
    { ...item, published_at: "nonsense" },
  ]) {
    assert.throws(() =>
      parseDigest({
        source_id: "pulse.zerodha",
        item_count: 1,
        items: [bad],
        memory_count: 0,
        memories: [],
      }),
    );
  }
});

test("ingest results must balance fetched = stored + duplicates", () => {
  assert.equal(
    parseIngestResult({
      source_id: "pulse.zerodha",
      fetched: 4,
      stored: 3,
      duplicates: 1,
      long_term_memories: 1,
    }).stored,
    3,
  );
  assert.throws(() =>
    parseIngestResult({
      source_id: "pulse.zerodha",
      fetched: 4,
      stored: 3,
      duplicates: 2,
      long_term_memories: 1,
    }),
  );
});
