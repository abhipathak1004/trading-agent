import assert from "node:assert/strict";
import test from "node:test";

import { parseDecisionPreview } from "../src/decisionContract.ts";
import { parsePrompt, parsePromptList } from "../src/promptsContract.ts";

const decision = {
  symbol: "NSE:RELIANCE",
  timeframe: "swing",
  as_of: "2026-11-26T09:30:00+00:00",
  decision_preview: "heuristic",
  data_provenance: {
    vendor_daily: "yahoo",
    vendor_quotes: "yahoo",
    is_realtime: false,
    data_freshness: "delayed_yfinance",
  },
  state: {
    schema_version: 1,
    symbol: "NSE:RELIANCE",
    timeframe: "swing",
    last_close: 1351,
    composite: 0.25,
    data_freshness: "delayed_yfinance",
  },
  judgments: [
    {
      name: "setup_direction",
      kind: "choice",
      choice: "long",
      score: null,
      noul: null,
      confidence: 0.5,
    },
    {
      name: "worth_deep_research",
      kind: "noul",
      choice: null,
      score: null,
      noul: 0.8,
      confidence: null,
    },
  ],
  screener: {
    verdict: "research",
    reason: "quality 0.80, worth 0.80",
    quality: 0.8,
    worth: 0.8,
    confidence: 0.5,
  },
  route: {
    tier: "heuristic",
    provider: "code",
    model: "heuristic",
    decision_source: "heuristic",
    fallback_used: true,
    fallback_from: "unavailable",
    latency_ms: 0,
    request_id: null,
    model_mismatch: false,
    state_hash: "abc",
  },
  notes: ["note"],
};

test("a valid decision preview is accepted", () => {
  const parsed = parseDecisionPreview(decision);
  assert.equal(parsed.symbol, "NSE:RELIANCE");
  assert.equal(parsed.judgments.length, 2);
  assert.equal(parsed.judgments[0].choice, "long");
  assert.equal(parsed.route.decision_source, "heuristic");
  assert.equal(parsed.screener.verdict, "research");
});

test("malformed decision previews are rejected", () => {
  for (const override of [
    { judgments: [] },
    { state: { nested: { a: 1 } } },
    { screener: { ...decision.screener, verdict: "moon" } },
    { route: { ...decision.route, fallback_used: "yes" } },
    { route: { ...decision.route, latency_ms: -1 } },
    {
      judgments: [
        {
          ...decision.judgments[0],
          noul: 1.5,
        },
      ],
    },
  ]) {
    assert.throws(() => parseDecisionPreview({ ...decision, ...override }));
  }
});

const prompt = {
  agent_id: "bull_debater",
  label: "Bull advocate",
  description: "Argues the long case.",
  system: "You are the bull.",
  template: "Symbol: {symbol}\n{facts}",
  required_fields: ["symbol", "facts"],
  version: 2,
  updated_at: "2026-11-26T12:00:00+00:00",
  is_default: false,
};

test("a prompt and prompt list are accepted", () => {
  const parsed = parsePrompt(prompt);
  assert.equal(parsed.agent_id, "bull_debater");
  assert.equal(parsed.version, 2);
  assert.deepEqual(parsed.required_fields, ["symbol", "facts"]);
  const list = parsePromptList({ prompts: [prompt] });
  assert.equal(list.length, 1);
});

test("malformed prompts are rejected", () => {
  for (const override of [
    { agent_id: "" },
    { required_fields: [1] },
    { version: -1 },
    { is_default: "yes" },
    { updated_at: "not-a-time" },
  ]) {
    assert.throws(() => parsePrompt({ ...prompt, ...override }));
  }
  assert.throws(() =>
    parsePromptList({ prompts: [prompt, { ...prompt, version: 3 }] }),
  );
  assert.throws(() => parsePromptList({ prompts: "no" }));
});
