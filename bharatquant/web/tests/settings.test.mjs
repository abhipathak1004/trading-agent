import assert from "node:assert/strict";
import test from "node:test";

import {
  ALL_PROVIDERS,
  PROVIDERS,
  parseConfig,
  parseOverview,
} from "../src/settingsContract.ts";

const validConfig = {
  provider: "GEMINI",
  model_name: "gemini-2.5-pro",
  news_feed_url: "https://pulse.zerodha.com/feed.xml",
  news_memory_ttl_days: 365,
  api_keys: [
    { provider: "GEMINI", configured: true, masked: "********abcd" },
  ],
  model_route_configured: true,
  updated_at: "2026-11-25T12:00:00Z",
};

const valid = {
  tasks: [
    {
      task_id: "confidence.review",
      task_version: "v1",
      task_class: "REASONING",
      allowed_data: ["PRIVATE", "PUBLIC"],
      allowed_tiers: ["FREE", "PAID"],
      prompt_version: "prompt-v1",
      spec_version: "spec-v1",
    },
  ],
  task_count: 1,
  schedule_count: 0,
  scheduler_running: true,
  model_usage_groups: 0,
  provider_connected: true,
  api_keys_configured: true,
  agent_configuration_state: "configured",
  config: validConfig,
};

test("a valid settings overview is accepted", () => {
  const parsed = parseOverview(valid);
  assert.equal(parsed.task_count, 1);
  assert.equal(parsed.tasks[0].task_id, "confidence.review");
  assert.deepEqual(parsed.tasks[0].allowed_data, ["PRIVATE", "PUBLIC"]);
  assert.equal(parsed.config.provider, "GEMINI");
  assert.equal(parsed.config.model_route_configured, true);
});

test("tasks cannot exceed the declared count or the 50-row cap", () => {
  assert.throws(
    () => parseOverview({ ...valid, task_count: 0 }),
    /Invalid settings overview/,
  );
  const many = Array.from({ length: 51 }, (_, index) => ({
    ...valid.tasks[0],
    task_id: `task-${index}`,
  }));
  assert.throws(
    () => parseOverview({ ...valid, tasks: many, task_count: 51 }),
    /Invalid settings overview/,
  );
});

test("overview provider state must match the configured route", () => {
  assert.throws(() =>
    parseOverview({
      ...valid,
      provider_connected: false,
      config: validConfig,
    }),
  );
  assert.throws(() =>
    parseOverview({
      ...valid,
      config: { ...validConfig, model_route_configured: false },
    }),
  );
});

test("malformed tasks are rejected", () => {
  for (const task of [
    { ...valid.tasks[0], task_id: "" },
    { ...valid.tasks[0], allowed_data: [] },
    { ...valid.tasks[0], allowed_tiers: [1] },
    { ...valid.tasks[0], prompt_version: "" },
    { ...valid.tasks[0], spec_version: "" },
  ]) {
    assert.throws(() => parseOverview({ ...valid, tasks: [task], task_count: 1 }));
  }
});

test("a valid configuration is accepted with no keys", () => {
  const parsed = parseConfig({
    ...validConfig,
    api_keys: [],
    model_route_configured: false,
    updated_at: null,
  });
  assert.deepEqual(parsed.api_keys, []);
  assert.equal(parsed.model_route_configured, false);
});

test("configuration rejects bad providers, retention and key states", () => {
  for (const override of [
    { provider: "ANTHROPIC" },
    { news_memory_ttl_days: 0 },
    { news_memory_ttl_days: 3651 },
    { news_memory_ttl_days: 1.5 },
    { updated_at: "not-a-time" },
    {
      api_keys: [{ provider: "GEMINI", configured: true, masked: null }],
    },
    {
      api_keys: [{ provider: "GEMINI", configured: false, masked: "********abcd" }],
    },
    {
      api_keys: [
        { provider: "GEMINI", configured: true, masked: "********abcd" },
        { provider: "GEMINI", configured: true, masked: "********wxyz" },
      ],
    },
  ]) {
    assert.throws(() => parseConfig({ ...validConfig, ...override }));
  }
});

// ---------------------------------------------------------------------------
// AWS Bedrock is additive: every existing provider is still accepted, and the
// new one carries a region.
// ---------------------------------------------------------------------------

test("bedrock is offered alongside every existing provider", () => {
  for (const name of ["GEMINI", "DEEPSEEK", "OPENAI", "CUSTOM", "BEDROCK"]) {
    assert.ok(PROVIDERS.includes(name), `${name} must remain selectable`);
  }
  assert.ok(ALL_PROVIDERS.includes("TYPESAFE_JEV"));
  assert.ok(ALL_PROVIDERS.includes("BEDROCK"));
});

test("a bedrock config carries its region", () => {
  const parsed = parseConfig({
    ...validConfig,
    provider: "BEDROCK",
    model_name: "us.anthropic.claude-sonnet-4-6",
    bedrock_region: "us-west-2",
  });
  assert.equal(parsed.provider, "BEDROCK");
  assert.equal(parsed.bedrock_region, "us-west-2");
});

test("a missing region reads as null, not a guess", () => {
  assert.equal(parseConfig({ ...validConfig }).bedrock_region, null);
  assert.equal(
    parseConfig({ ...validConfig, bedrock_region: "" }).bedrock_region,
    null,
  );
});
