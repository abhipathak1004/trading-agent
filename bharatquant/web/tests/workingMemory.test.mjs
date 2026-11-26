import assert from "node:assert/strict";
import test from "node:test";

import { parseWorkingMemory } from "../src/workingMemoryContract.ts";

const response = {
  run_id: "run-1",
  memories: [
    {
      run_id: "run-1",
      symbol: "NSE:RELIANCE",
      source: "analysis",
      as_of: "2026-11-26T12:00:00Z",
      schema_version: 1,
      state: { mode: "RULES_ONLY", technical: { composite: 0.4 } },
      state_hash: "abc123",
      recorded_at: "2026-11-26T12:01:00Z",
    },
  ],
};

test("a working-memory response parses", () => {
  const parsed = parseWorkingMemory(response);
  assert.equal(parsed.memories[0].symbol, "NSE:RELIANCE");
  assert.equal(parsed.memories[0].state.mode, "RULES_ONLY");
});

test("malformed working memory is rejected", () => {
  assert.throws(() => parseWorkingMemory({ ...response, memories: [] }));
  assert.throws(() =>
    parseWorkingMemory({
      ...response,
      memories: [{ ...response.memories[0], state_hash: "" }],
    }),
  );
  assert.throws(() =>
    parseWorkingMemory({
      ...response,
      memories: [{ ...response.memories[0], state: null }],
    }),
  );
});
