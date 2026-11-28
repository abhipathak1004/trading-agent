import assert from "node:assert/strict";
import test from "node:test";

import {
  isPulling,
  parseLocalModel,
  parseLocalModels,
  pullTone,
} from "../src/modelsContract.ts";

const model = {
  id: "ollama:chat:gemma4:12b-mlx",
  label: "Gemma 4 12B (MLX)",
  kind: "ollama",
  model: "gemma4:12b-mlx",
  endpoint: null,
  installed: true,
  pull: null,
};

test("a local model list parses", () => {
  const models = parseLocalModels({ models: [model] });
  assert.equal(models.length, 1);
  assert.equal(models[0].id, "ollama:chat:gemma4:12b-mlx");
  assert.equal(models[0].installed, true);
  assert.equal(models[0].pull, null);
});

test("openai-compatible models keep their endpoint and unknown install state", () => {
  const parsed = parseLocalModel({
    id: "openai:chat:mlx-community/qwen",
    label: "MLX",
    kind: "openai_compatible",
    model: "mlx-community/qwen",
    endpoint: "http://127.0.0.1:8080/v1",
    installed: null,
    pull: { status: "running", output: "pulling..." },
  });
  assert.equal(parsed.endpoint, "http://127.0.0.1:8080/v1");
  assert.equal(parsed.installed, null);
  assert.equal(isPulling(parsed), true);
  assert.equal(pullTone(parsed.pull?.status), "warn");
});

test("pull tones map terminal states", () => {
  assert.equal(pullTone("completed"), "ok");
  assert.equal(pullTone("failed"), "danger");
  assert.equal(pullTone(undefined), "muted");
});

test("malformed local models are rejected", () => {
  assert.throws(() => parseLocalModels({ models: "no" }));
  for (const override of [
    { id: "" },
    { kind: "moon" },
    { model: "" },
    { endpoint: 12 },
    { installed: "yes" },
    { pull: { nope: 1 } },
  ]) {
    assert.throws(() => parseLocalModel({ ...model, ...override }));
  }
});
