import assert from "node:assert/strict";
import test from "node:test";

import { parseConfig, parseModelsConfig } from "../src/settingsContract.ts";

const tier = {
  provider: "GEMINI",
  model: "gemini-2.5-flash",
  temperature: 0.1,
  max_tokens: 2048,
  timeout_ms: 60000,
  max_attempts: 3,
  fallback_tier: null,
  configured: true,
};

const valid = {
  tiers: {
    decision: {
      ...tier,
      provider: "TYPESAFE_JEV",
      model: "jev-1.14",
      temperature: 0,
      max_tokens: 1,
      timeout_ms: 3000,
      max_attempts: 1,
      fallback_tier: "flash",
    },
    reasoning: { ...tier, model: "gemini-2.5-pro", fallback_tier: null },
    flash: { ...tier, fallback_tier: "reasoning" },
  },
  agent_routing: {
    bull_debater: { tier: "reasoning", provider: null, model: null },
    risk_analyst: { tier: "flash", provider: null, model: null },
  },
  decision_policy: {
    min_choice_confidence: 0.55,
    min_noul_probability: 0.6,
    abstain_score_band: [2, 3],
    llm_fallback_size_scale: 0.75,
    heuristic_size_scale: 0.5,
  },
  pipeline: {
    debate_rounds: 1,
    max_tool_rounds_per_agent: 8,
    screener_min_score: 0.6,
    final_decision_min_prob: 0.65,
    execution_mode: "PAPER",
    decision_fallback_order: ["flash", "reasoning", "heuristic"],
  },
  decision_route: "auto",
  configured_tiers: ["decision", "reasoning", "flash"],
  decision_preview: "jev",
};

test("a valid models configuration is accepted", () => {
  const parsed = parseModelsConfig(valid);
  assert.equal(parsed.tiers.decision.provider, "TYPESAFE_JEV");
  assert.equal(parsed.tiers.decision.fallback_tier, "flash");
  assert.equal(parsed.agent_routing.bull_debater.tier, "reasoning");
  assert.equal(parsed.pipeline.decision_fallback_order[0], "flash");
  assert.equal(parsed.decision_preview, "jev");
});

test("missing tiers, bad previews and non-snake tiers are rejected", () => {
  assert.throws(() =>
    parseModelsConfig({
      ...valid,
      tiers: { decision: valid.tiers.decision, reasoning: valid.tiers.reasoning },
    }),
  );
  assert.throws(() => parseModelsConfig({ ...valid, decision_preview: "magic" }));
  assert.throws(() =>
    parseModelsConfig({
      ...valid,
      agent_routing: { bad: { tier: "moon", provider: null, model: null } },
    }),
  );
  assert.throws(() =>
    parseModelsConfig({
      ...valid,
      tiers: { ...valid.tiers, flash: { ...tier, temperature: 3 } },
    }),
  );
});

test("fallback order must be non-empty and unique", () => {
  assert.throws(() =>
    parseModelsConfig({
      ...valid,
      pipeline: { ...valid.pipeline, decision_fallback_order: [] },
    }),
  );
  assert.throws(() =>
    parseModelsConfig({
      ...valid,
      pipeline: {
        ...valid.pipeline,
        decision_fallback_order: ["flash", "flash"],
      },
    }),
  );
});

test("the abstain band must be ordered and probabilities bounded", () => {
  assert.throws(() =>
    parseModelsConfig({
      ...valid,
      decision_policy: {
        ...valid.decision_policy,
        abstain_score_band: [3, 2],
      },
    }),
  );
  assert.throws(() =>
    parseModelsConfig({
      ...valid,
      decision_policy: {
        ...valid.decision_policy,
        heuristic_size_scale: 1.5,
      },
    }),
  );
});

test("a TypeSafe Jev key is accepted in the legacy config payload", () => {
  const parsed = parseConfig({
    provider: "GEMINI",
    model_name: "gemini-2.5-pro",
    news_feed_url: "https://pulse.zerodha.com/feed.xml",
    news_memory_ttl_days: 365,
    api_keys: [
      { provider: "TYPESAFE_JEV", configured: true, masked: "********abcd" },
    ],
    model_route_configured: false,
    updated_at: null,
  });
  assert.equal(parsed.api_keys[0].provider, "TYPESAFE_JEV");
});
