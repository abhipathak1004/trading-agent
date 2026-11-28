# Local Coding Agent Implementation Prompt — Model Evals Tab

Copy and paste the prompt below directly into your local coding agent (Claude Code, Codex, Cursor, Windsurf, etc.) after unzipping `local_llm_eval_tab_bundle.zip` into your repository root.

---

```markdown
You are implementing a new **"Model Evals"** tab inside this trading application (BharatQuant Agents) so I can benchmark local Apple Silicon LLMs (e.g., Gemma 4 12B, Qwen3.5-9B, Granite 4.2 8B) directly from the UI, inspect per-category pass rates, p95 latency, and Brier calibration scores, and optionally promote a winning model into `config/agent_settings.yaml`.

First, inspect the existing repository structure, backend framework (`bq serve` / worker pool / SQLite WAL layer), `config/agent_settings.yaml`, and frontend SPA (React 19 + TypeScript + Vite dark theme) so every new file, route, style token, and database migration follows existing project conventions.

## 1. Included Bundle Files (Already Placed in `evals/`)

Verify these three files exist in `evals/` (move them there if unzipped at repo root):

1. `evals/promptfooconfig.yaml`
   - Promptfoo evaluation configuration targeting local Ollama (`ollama:chat:gemma4:12b-mlx`, `ollama:chat:qwen3.5:9b`, `ollama:chat:granite4.2:8b`) with a commented-out `openai:chat:*` template for direct `mlx_lm.server` endpoints (`http://127.0.0.1:8080/v1`).
   - Declares two OpenAI-format trading tools (`get_ohlcv_snapshot` with required `symbol`, `timeframe`, `as_of`; `get_corporate_filings` with `symbol`, `category`, `limit`).
   - Loads `file://trading_golden_eval_50.csv` and runs two `defaultTest` assertions: `latency_under_10s` (10,000 ms threshold) and `golden_task_accuracy` (strict JSON schema validation, enum checks, number grounding, noise confidence caps, and tool-call / abstention verification).

2. `evals/trading_golden_eval_50.csv`
   - 50-case golden dataset (10 cases per category via `__metadata:category`):
     - `earnings` (10): NSE/BSE quarterly results, beats/misses, guidance revisions.
     - `rbi_macro` (10): MPC rate decisions, CRR/LCR/risk-weight circulars, OMO liquidity, regulatory orders.
     - `corporate_action` (10): bonus issues, buybacks, splits, demergers, promoter block deals, QIPs, rights issues.
     - `noise_filtering` (10): unconfirmed rumours, generic wraps, and zero-number items (asserting `extracted_numbers == []` and capped confidence).
     - `edge_tool_calls` (10): point-in-time `as_of` OHLCV calls, filing lookups, plus abstention cases (`expected_tool == NONE`) for missing timestamps, ambiguous tickers, and unsupported live-order requests.

3. `evals/score_promptfoo_results.py`
   - Zero-dependency Python 3 module and CLI that parses Promptfoo's `results.json`.
   - Exposes reusable functions: `load_result_items(payload)`, `analyze_results(items)` -> `(summaries, sorted_cats)`, and `export_csv(summaries, categories, out_path)`.
   - Computes per-model: `overall_pass_rate`, `accuracy_pass_rate`, `json_parse_rate`, `p50_ms`, `p95_ms`, `max_ms`, `brier_score` (on `news_extraction` JSON `confidence` vs. `golden_task_accuracy`), `ece_5bin` (5-bin Expected Calibration Error), `brier_n`, and per-category pass rates (`cat_stats`, `cat_rates`).

## 2. Backend Implementation Requirements

1. **SQLite (WAL) Persistence**
   - Add a migration creating two tables (matching existing migration style):
     - `eval_runs`: `id` (UUID/TEXT PK), `created_at`, `started_at`, `completed_at`, `status` (`queued|running|completed|failed|cancelled`), `models_json`, `categories_json`, `config_path`, `results_json_path`, `error_message`.
     - `eval_model_scores`: `id` (PK), `run_id` (FK -> `eval_runs.id`), `model`, `total_cases`, `overall_pass_rate`, `accuracy_pass_rate`, `json_parse_rate`, `p50_ms`, `p95_ms`, `max_ms`, `brier_score`, `ece_5bin`, `brier_n`, `category_rates_json`.

2. **Memory-Safe Background Execution Worker (24 GB MacBook Pro Constraint)**
   - Run Promptfoo asynchronously in the existing worker pool (never block HTTP handlers).
   - Enforce a **single-active-eval lock** so only one benchmark runs at a time, and ALWAYS pass `--max-concurrency 1` and `PROMPTFOO_DISABLE_TELEMETRY=1` so only one local LLM loads into 24 GB unified memory at once:
     ```bash
     PROMPTFOO_DISABLE_TELEMETRY=1 npx promptfoo@latest eval \
       --max-concurrency 1 \
       --no-cache \
       -c evals/promptfooconfig.yaml \
       -o <run_artifact_dir>/results.json
     ```
   - Support optional `--filter-providers` when the user selects a subset of models in the UI.
   - Upon completion, import `load_result_items` and `analyze_results` from `evals.score_promptfoo_results` (avoid shelling out twice), persist `eval_model_scores`, and also write `scorecard.csv` into `<run_artifact_dir>/`.
   - Support graceful cancellation (`SIGTERM` to the subprocess group) and mark status `cancelled`.

3. **REST API Endpoints**
   - `GET /api/evals/config` — Returns available providers parsed from `evals/promptfooconfig.yaml`, available categories from `evals/trading_golden_eval_50.csv`, and whether `npx`/`ollama` are reachable locally.
   - `GET /api/evals/runs` — Lists historical eval runs with summary scores (newest first).
   - `POST /api/evals/runs` — Starts a new eval run (`{ providers?: string[], categories?: string[], no_cache?: boolean }`), returning `409 Conflict` if another run is currently active.
   - `GET /api/evals/runs/{run_id}` — Returns run status, per-model scorecard (`eval_model_scores`), and category breakdown.
   - `GET /api/evals/runs/{run_id}/cases?model=...&category=...&status=failed|passed|all` — Reads `<run_artifact_dir>/results.json` and returns case-level inputs, raw outputs, extracted confidence, latency, and assertion failure reasons.
   - `POST /api/evals/runs/{run_id}/cancel` — Cancels an active run.
   - `POST /api/evals/promote` — Updates a target agent role's local model entry in `config/agent_settings.yaml` (validate model exists and write atomically via temp file + rename; return before/after diff).

## 3. Frontend Implementation Requirements (React 19 + TypeScript + Vite Dark Theme)

Add a **"Model Evals"** tab in the main navigation bar routing to `/evals` (matching existing dark-theme design tokens, typography, and card/table primitives):

1. **Run Launcher & Status Bar**
   - Multi-select checkboxes for candidate local models (from `/api/evals/config`) and optional category filters.
   - Visible badge/note confirming sequential execution (`max-concurrency: 1`) for 24 GB RAM safety.
   - "Run Benchmark" button (disabled while a run is active) + "Cancel Run" button + live polling every 3s while `status === 'running'`.

2. **Model Scorecard & Category Breakdown**
   - Run selector dropdown (defaults to latest completed run).
   - Primary comparison table showing per-model: **Overall Pass %**, **Task Accuracy %**, **JSON Parse %**, **p50 / p95 Latency**, **Brier Score (lower is better)**, **ECE-5**, and **Calib N**, highlighting the best value per column.
   - Category pass-rate matrix (`earnings`, `rbi_macro`, `corporate_action`, `noise_filtering`, `edge_tool_calls`) with color-coded cells (green >= 85%, amber 65–84%, red < 65%).
   - Download button for `scorecard.csv`.

3. **Case-Level Failure Inspector Drawer**
   - Clicking any category cell or "Inspect Cases" opens a filterable drawer/table (`All / Failed / Passed`, filter by model & category) showing test description, prompt input, raw model JSON or tool-call output, latency, and exact assertion failure reason.

4. **Promote Winning Model Action**
   - "Promote Model" button per row opening a confirmation modal where I pick the target agent role (e.g., `news_research_agent`, `triage_small_model`), preview the `config/agent_settings.yaml` change, and confirm before calling `POST /api/evals/promote`.

## 4. Verification & Acceptance Criteria

Before finishing, run and report the results of:
1. **Backend tests**: Add pytest tests covering (a) `score_promptfoo_results.py` parsing a fixture `results.json`, (b) SQLite migration & CRUD for `eval_runs`/`eval_model_scores`, (c) single-run concurrency lock (`409`), and (d) `/api/evals/runs/{run_id}/cases` filtering.
2. **Frontend checks**: Run `npm run typecheck` and `npm run build` (or the repo's equivalent Vite build script) with zero TypeScript or bundler errors.
3. **End-to-end smoke check**: Seed one mock completed run from a sample `results.json` and confirm the `/evals` tab renders the scorecard, category matrix, and failure inspector cleanly with no console errors.
```
