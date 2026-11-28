import assert from "node:assert/strict";
import test from "node:test";

import {
  parseAgentRun,
  parseAgentRunDetail,
  parseAgentRunHistory,
  parseAgentStatus,
} from "../src/agentContract.ts";

const status = {
  agent_id: "research",
  configured: true,
  provider: "GEMINI",
  model: "gemini-2.5-pro",
  tools: [
    {
      name: "news_search",
      summary: "Search news",
      mutating: false,
      arguments: ["query", "limit"],
    },
  ],
};

const run = {
  task: "What did the RBI do?",
  final_text: "The RBI held the repo rate.",
  turns: 2,
  stopped: true,
  tool_calls: ["news_search", "response"],
  steps: [
    {
      turn: 1,
      kind: "tool",
      tool: "news_search",
      arguments: { query: "rbi" },
      output: "1 result",
    },
  ],
  provider: "GEMINI",
  model: "gemini-2.5-pro",
  input_tokens: 100,
  output_tokens: 40,
};

test("valid agent status is accepted", () => {
  const parsed = parseAgentStatus(status);
  assert.equal(parsed.agent_id, "research");
  assert.equal(parsed.tools[0].name, "news_search");
  assert.equal(parsed.configured, true);
});

test("valid agent run is accepted", () => {
  const parsed = parseAgentRun(run);
  assert.equal(parsed.final_text, "The RBI held the repo rate.");
  assert.equal(parsed.steps[0].tool, "news_search");
  assert.equal(parsed.input_tokens, 100);
});

test("agent run accepts null token counts", () => {
  const parsed = parseAgentRun({ ...run, input_tokens: null, output_tokens: null });
  assert.equal(parsed.input_tokens, null);
});

test("malformed agent status is rejected", () => {
  for (const bad of [
    { ...status, configured: "yes" },
    { ...status, tools: [{ name: "x", summary: "y", mutating: false, arguments: [1] }] },
    { ...status, agent_id: "" },
  ]) {
    assert.throws(() => parseAgentStatus(bad));
  }
});

test("malformed agent run is rejected", () => {
  for (const bad of [
    { ...run, stopped: "yes" },
    { ...run, steps: [{ ...run.steps[0], kind: "weird" }] },
    { ...run, tool_calls: [1] },
    { ...run, turns: -1 },
  ]) {
    assert.throws(() => parseAgentRun(bad));
  }
});

const historyItem = {
  run_id: "run-1",
  agent_id: "research",
  task: "What did the RBI do?",
  final_text: "The RBI held the repo rate.",
  turns: 2,
  stopped: true,
  provider: "GEMINI",
  model: "gemini-2.5-pro",
  input_tokens: 100,
  output_tokens: 40,
  started_at: "2026-11-28T12:00:00+00:00",
  finished_at: "2026-11-28T12:00:01+00:00",
};

test("agent run history and detail parse", () => {
  const history = parseAgentRunHistory({ runs: [historyItem] });
  assert.equal(history.length, 1);
  assert.equal(history[0].run_id, "run-1");
  const detail = parseAgentRunDetail({ ...historyItem, steps: run.steps });
  assert.equal(detail.steps[0].tool, "news_search");
});

test("malformed agent history is rejected", () => {
  assert.throws(() => parseAgentRunHistory({ runs: [{ ...historyItem, run_id: "" }] }));
  assert.throws(() => parseAgentRunDetail({ ...historyItem, steps: [1] }));
});
