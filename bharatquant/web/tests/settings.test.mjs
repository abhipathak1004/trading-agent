import assert from "node:assert/strict";
import test from "node:test";

import { parseOverview } from "../src/settingsContract.ts";

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
  provider_connected: false,
  api_keys_configured: false,
  agent_configuration_state: "not_configured",
};

test("a valid settings overview is accepted", () => {
  const parsed = parseOverview(valid);
  assert.equal(parsed.task_count, 1);
  assert.equal(parsed.tasks[0].task_id, "confidence.review");
  assert.deepEqual(parsed.tasks[0].allowed_data, ["PRIVATE", "PUBLIC"]);
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

test("a connected provider or configured keys are rejected", () => {
  for (const override of [
    { provider_connected: true },
    { api_keys_configured: true },
    { agent_configuration_state: "configured" },
  ]) {
    assert.throws(() => parseOverview({ ...valid, ...override }));
  }
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
