import assert from "node:assert/strict";
import test from "node:test";

import {
  bestValue,
  formatMs,
  formatPercent,
  formatScore,
  isBest,
  parseEvalCases,
  parseEvalConfig,
  parseEvalRun,
  parseEvalRuns,
  parsePromoteDiff,
  rateTone,
  runStatusTone,
} from "../src/evalContract.ts";

const SCORE = {
  model: "Qwen3.5-9B",
  total_cases: 50,
  overall_pass_rate: 0.9,
  accuracy_pass_rate: 0.88,
  json_parse_rate: 1.0,
  p50_ms: 1200,
  p95_ms: 4100,
  max_ms: 9000,
  brier_score: 0.03,
  ece_5bin: 0.05,
  brier_n: 50,
  category_rates: { earnings: 0.9, noise_filtering: 0.6 },
};

function run(overrides = {}) {
  return {
    id: "run-1",
    created_at: "2026-11-26T12:00:00Z",
    started_at: "2026-11-26T12:00:01Z",
    completed_at: "2026-11-26T12:40:00Z",
    status: "completed",
    models: ["Qwen3.5-9B"],
    categories: ["earnings"],
    config_path: "evals/promptfooconfig.yaml",
    results_json_path: "/tmp/results.json",
    error_message: null,
    scores: [SCORE],
    ...overrides,
  };
}

test("eval config parses providers, categories and tools", () => {
  const config = parseEvalConfig({
    providers: [
      { id: "ollama:chat:qwen3.5:9b", label: "Qwen3.5-9B", model: "qwen3.5:9b" },
    ],
    categories: ["earnings", "rbi_macro"],
    tools: { npx: true, ollama: false },
    config_path: "evals/promptfooconfig.yaml",
    dataset_path: "evals/trading_golden_eval_50.csv",
    active_run_id: null,
    custom_endpoint: null,
  });
  assert.equal(config.providers[0].model, "qwen3.5:9b");
  assert.deepEqual(config.categories, ["earnings", "rbi_macro"]);
  assert.equal(config.tools.npx, true);
  assert.equal(config.tools.ollama, false);
});

test("eval run and runs list parse with scores", () => {
  const parsed = parseEvalRun(run());
  assert.equal(parsed.status, "completed");
  assert.equal(parsed.scores[0].p95_ms, 4100);
  assert.equal(parsed.scores[0].category_rates.noise_filtering, 0.6);

  const list = parseEvalRuns({ runs: [run(), run({ id: "run-2", status: "failed" })] });
  assert.equal(list.length, 2);
  assert.equal(list[1].status, "failed");
});

test("cases parse with failure reasons", () => {
  const parsed = parseEvalCases({
    run_id: "run-1",
    model: "Gemma 4 12B (MLX)",
    category: "noise_filtering",
    status: "failed",
    total: 1,
    cases: [
      {
        description: "Noise 01",
        model: "Gemma 4 12B (MLX)",
        category: "noise_filtering",
        passed: false,
        input: "rumour",
        raw_output: "{}",
        confidence: 0.55,
        latency_ms: 8000,
        failure_reasons: ["confidence too high"],
      },
    ],
  });
  assert.equal(parsed.total, 1);
  assert.equal(parsed.cases[0].passed, false);
  assert.deepEqual(parsed.cases[0].failure_reasons, ["confidence too high"]);
});

test("promote diff parses before/after routes", () => {
  const diff = parsePromoteDiff({
    agent_id: "news_research",
    before: { tier: "flash", provider: null, model: null },
    after: { tier: "flash", provider: "CUSTOM", model: "qwen3.5:9b" },
    yaml_before: "agents:\n",
    yaml_after: "agents:\n  news_research:\n",
  });
  assert.equal(diff.after.model, "qwen3.5:9b");
  assert.equal(diff.before.model, null);
});

test("category tone thresholds are green/amber/red", () => {
  assert.equal(rateTone(0.9), "ok");
  assert.equal(rateTone(0.85), "ok");
  assert.equal(rateTone(0.7), "warn");
  assert.equal(rateTone(0.64), "danger");
  assert.equal(rateTone(null), "muted");
});

test("run status tone maps terminal states", () => {
  assert.equal(runStatusTone("completed"), "ok");
  assert.equal(runStatusTone("running"), "warn");
  assert.equal(runStatusTone("failed"), "danger");
  assert.equal(runStatusTone("cancelled"), "danger");
});

test("best value respects higher-is-better and lower-is-better", () => {
  const scores = parseEvalRun(
    run({ scores: [SCORE, { ...SCORE, model: "other", p95_ms: 3000, overall_pass_rate: 0.8 }] }),
  ).scores;
  assert.equal(bestValue(scores, "overall_pass_rate"), 0.9);
  assert.equal(bestValue(scores, "p95_ms"), 3000);
  assert.equal(isBest(scores, "p95_ms", 3000), true);
  assert.equal(isBest(scores, "p95_ms", 4100), false);
});

test("formatters render percents, ms and scores", () => {
  assert.equal(formatPercent(0.9), "90.0%");
  assert.equal(formatPercent(null), "—");
  assert.equal(formatMs(1234.5), "1235 ms");
  assert.equal(formatScore(0.0345), "0.035");
  assert.equal(formatScore(null), "—");
});
