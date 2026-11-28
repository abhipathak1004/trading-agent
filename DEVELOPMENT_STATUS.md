# BharatQuant — compact handoff

Updated 28 November 2026. This file keeps only what is needed for the **next**
task. History lives in git and
[`architecture-decisions.md`](./bharatquant/docs/architecture-decisions.md).

## Current state

- **Research Lab, Phases 0–5 (28 Nov 2026, ADR-054).** Plan:
  `~/.commandcode/plans/research-lab-vibe-trading-replan.md`. Polars/PyArrow/
  DuckDB are runtime deps as a **derived** analytical stack; SQLite stays the
  only transactional authority and Postgres was measured and declined (47
  stores, 42 migrations, ~1,110 tests; see plan §1). Migration **043** adds
  `research_sessions`, `research_runs`, `portfolio_snapshots`,
  `research_artifacts`, `research_evidence`, `execution_mandates` — no
  `audit_ledger` (`audit_events` is reused) and no FTS in SQL (lazy + LIKE
  fallback, matching `semantic_docs_fts`).
  - `bq/research_lab/`: `config` in `settings.py` (`save_research_lab`,
    `resolve_research_lab_models`, `parse_research_lab`; overrides are in-memory
    only); `contracts.py` (Int64-paise contract, refuses mixed raw/adjusted and a
    network-served `local`); `freeze.py` (two digests: content identity + file
    tamper); `store.py` (sessions/runs/versioned artifacts); `replay.py`
    (verified replay + walk-forward folds/holdout, CSV); `risk_xray.py` (top-5
    episodes + open, signed contributions summing to total vol); `alpha_bench.py`
    (IC/rank IC/ICIR/turnover + survivorship stamp); `reconcile.py`;
    `evidence.py` (scoped E_t, 24/24 ablation); `security.py` (path confinement,
    no shell/network surface); `prompting.py` (byte-stable prefix);
    `mandate.py` (paper-only gate, kill switch, existing audit chain);
    `harness.py` (bounded loop: tool → admit → gate → bounded recovery);
    `runner.py` (events.jsonl journal + cursor reads + single-flight + cancel);
    `routes.py` (`/api/v1/research/*`, SSE via journal, `ResearchDeps`).
  - **API:** sessions, messages (runs the harness), cancelled runs keep a partial
    trace, run detail (equity + Risk X-Ray + evidence), replay + CSV, alpha/bench,
    paper/reconcile, versioned artifacts (FTS search, diff, export), and
    `GET/PUT /models`. Registered before the SPA catch-all; `/api/v1/research/`
    was already in the `no-store` prefixes. `create_app` gained an optional
    `research_model_turn` for offline tests.
  - **UI:** new **Analyze → Research Lab** tab (`web/src/ResearchLabPanel.tsx`,
    `researchLabContract.ts`) with Workbench / Run Detail / Artifacts / Alpha
    Bench, plus a **Research Lab Agents** section inside Settings → Models & keys.
    The existing `research` view (dossiers) is untouched; the new key is `lab`.
  - `paper_charges.py` gained brokerage/IPFT/DP/venue and a **corrected GST base**;
    all new rates default to 0 so no existing number moved. `backtest.py` is
    product-aware (was hardcoded MIS) and reports `sharpe`/`sortino`/
    `profit_factor` as `None`, never NaN.
  - **Verification:** backend 1321 pass with only the 2 pre-existing failures;
    ruff + mypy clean on `bq/research_lab`; frontend 181 tests, typecheck and
    production build clean; `bq init` on a scratch `BQ_HOME` applies **43**
    migrations. **Not yet done:** a live browser smoke against the real DB (it
    needs 043 applied and a model route configured).
- **AI Hedge Fund is being ported in-process (28 Nov 2026, IN PROGRESS).** Plan:
  `~/.commandcode/plans/aihf-in-process-port.md`, supersedes ADR-050. The sidecar
  exists only because `aihf==2.7.2` pins `pandas<3`/`numpy<2`; in fact pandas is
  **dead code** in it (a never-called `_compute_rsi`) and numpy is 4 files of
  scalar stats, so the whole engine now runs inside this venv on our own data and
  models. **ADR-052 is not written yet.**
  - **Phase 1 DONE.** `bq/aihf/engine/` is vendored `hedge_fund` (MIT, LICENSE +
    `PATCHES.md`). No pandas/numpy/langchain/textual/requests; `bq/aihf/engine/api.py`
    replaces `run.py`. `paths.py` root is injected (`<BQ_HOME>/aihf`). Ken-French
    breakpoints fetch removed. New dep: **pyyaml** (mandate YAML). Guarded by
    `tests/test_aihf_engine_imports.py`, which imports the engine in a subprocess
    and asserts no forbidden root is loaded.
  - **Phase 2 DONE.** `bq/aihf/bridge/calendar.py` (IST, NSE holidays, weekday
    fallback via `ExchangeCalendar`), `bridge/clock.py` (`NseDailyClock`;
    installed into the engine through `clock.set_clock_factory`), `bridge/costs.py`
    (`IndianEquityCosts` → `paper_charges.compute_charges`, matches to the paise).
    `load_spec` refuses minute cadence + `universe:`; benchmark default is
    `NIFTY 50`. `tests/test_aihf_market.py`.
  - **Phase 3 DONE.** Migration **042_aihf_data.sql** (rebuilds
    `fundamental_snapshots` to add `period_label` + `YAHOO` source, verified
    incrementally 41→42; new `company_facts`, `earnings_events`).
    `bq/aihf/data_store.py` (`CompanyFactsStore`, `EarningsStore`),
    `bq/data/vendors/yahoo_fundamentals_vendor.py` (real reported quarterly
    statements, `known_at = period_end + 45d`, skips quarters Yahoo cannot
    support), `bq/aihf/bridge/data.py` (the engine's `DataClient` over the vendor
    router + PIT stores), `bq/aihf/ingest.py` + job
    `aihf.fundamentals.ingest` (disabled by default). Tests:
    `test_aihf_bridge_data.py`, `test_aihf_fundamentals_ingest.py`,
    `test_aihf_ingest.py`.
  - **Phase 4 DONE.** `bq/aihf/bridge/llm.py` (`RouterLLM` over `ModelRouter` +
    `complete_structured`), `bridge/personas.py` (`staff()` injects per-persona
    clients + editable prompts), `bq/aihf/persona_prompts.py` (dependency-free
    default text, asserted equal to the persona classes). Five `aihf_*` agent ids
    in `DEFAULT_AGENT_ROUTING` + a **merge** so an existing DB gains them, agent
    catalogue entries, editable `PromptDefinition`s, `MAX_AGENT_ROUTING_ENTRIES`
    32→48. `LLMAgent` gained `system_prompt=` and `voice()`.
    `tests/test_aihf_bridge_llm.py`.
  - **Phase 5 DONE.** `runner.py` no longer owns a process: `AiHfRunner.start`
    takes a callable body that runs in a worker thread with cooperative
    cancellation, and the same `Progress` journal keys drive the UI and SSE.
    `config.py` dropped the venv (`installed_version()` = `"in-process"`);
    `runtime_env.py` is a pure key-free descriptor (no env building);
    `registry.py`, `probe/` deleted. **The approval gate is now a true dry run:**
    `PaperStore.preview` rebuilds the broker in memory and calls
    `engine.predict_session`, writing nothing to the fund; `execute` appends that
    exact record and `Ledger.append` re-verifies the hash and chain. No sandbox,
    no `directory_digest`, one LLM spend per session. `routes.py` runs the
    backtest in-process (`_backtest_body` → `engine.run_backtest`), refuses
    `model_override` with 422, and dropped the Financial-Datasets key routes.
    `api.py` installs the NSE clock at startup. New `bq/aihf/wiring.py` builds the
    data client. Tests rewritten: `test_aihf_api.py`, `test_aihf_runtime_env.py`,
    `test_aihf_parsing.py`; new `tests/test_aihf_gate.py` (preview writes nothing,
    tampered/stale preview refused, broker matches record).
  - **Phase 6 DONE.** UI: `AiHedgeFundPanel` drops the sidecar banner, the
    `EXTERNAL ENGINE` eyebrow, and the model-override select; copy now says the
    engine is in-process and prices sessions as "computed without touching the
    fund". `SettingsConfigPanel` replaces the Financial-Datasets key section with
    a note (no data key exists). `just aihf-install`/`aihf-status` removed;
    `.gitignore` drops `.aihf-venv`; README rewritten. **ADR-053** added and
    ADR-050 marked superseded (ADR-052 was already taken by tool calling).
  - **Phase 7 DONE.** Verified: **migration 042 applied to a copy of the owner DB
    (at 41, with 6 settings keys and the live `paper-main` account) → 42, all
    preserved, integrity ok.** Persona routes merge into the real routing row
    (existing routes kept, 20 total, `aihf_buffett` → reasoning/GEMINI). Live
    server on a throwaway DB: status/version/models correct, mandate create 201,
    minute + universe mandates 422, fund create 201 on RELIANCE/TCS, halt →
    preview AND execute both 409 → resume → ledger `chain_verified`. End-to-end
    engine backtest trades and charges ₹505 on a ₹10L round trip (matches
    `paper_charges`). **Fixed a real defect found only by running it:** PEAD
    accepted only SEC `8-K` and so abstained on every Indian `RESULT` filing —
    now `RESULT` is an announcement source (`tests/test_aihf_pead.py`).
  - **Import graph:** the vendored engine loads **no** pandas, numpy, langchain,
    textual or requests (subprocess probe). `pyyaml` is the one new dependency.
  - **Next:** nothing outstanding for this port. Intraday funds, `universe:`
    screening, a second market, and a scheduler job to advance funds unattended
    are future work.
  - **Next migration number is `043`.**
  - Suite: 1205 pass; the same 2 pre-existing failures (`test_logbook`,
    `test_phase5_acceptance`). mypy: zero errors in `bq/aihf/**` and
    `yahoo_fundamentals_vendor.py`; the 49 remaining are the documented
    pre-existing ones. Frontend: 174 tests, typecheck and build pass.

- **Native tool calling (28 Nov 2026):** agents now get a real tool protocol
  instead of prose. `ToolSpec.input_schema()` renders one JSON Schema;
  `complete()` takes `tools`/`tool_call`/`tool_result`; Bedrock nests them under
  `toolSpec.inputSchema.json`, OpenAI uses `tools`/`tool_calls`, Gemini uses
  `functionDeclarations`. **The round trip was missing for every provider** —
  a native call's result went to text history only, so turn 2 had no record of
  the call and re-requested the same tool. `run_agent_loop` now stages the
  executed call and result for the closure to replay exactly once. Verified
  against a capturing mock: 9 tool specs reach `converse`, and turn 2 sends
  `assistant[toolUse]` → `user[toolResult]` → prompt with no repeat call.
  `map_finish_reason` learned `TOOL_USE`. ADR-052.
  - Agents are configured in one step: `POST /api/v1/settings/bedrock/apply`
    (UI: **Use Bedrock for all text agents**) points the reasoning and flash
    tiers at a Bedrock model — **11 of 15 agent roles**. The 4 decision agents
    stay on TypeSafe Jev by design. Verified live: `/api/v1/agents` resolves
    7 text roles to BEDROCK, 4 to TYPESAFE_JEV.
  - The prose `tools_prompt()` stays in the prompt as the fallback path, so a
    provider without native tool use keeps working.
  - Test doubles for `ModelClient`/`ModelRouter` must accept the new optional
    kwargs; `tests/test_bedrock_tools.py` covers schemas, both wire shapes and
    the loop.
- **AWS Bedrock provider (28 Nov 2026):** `Provider.BEDROCK` is an **additive**
  sixth provider — the five existing ones, their keys and the active selection
  are untouched (asserted by test). Bedrock API keys are **bearer tokens**, not
  AWS secret keys, so `BedrockClient` sends `Authorization: Bearer` and does no
  SigV4. It speaks **`Converse`**, a third wire shape. Migration **041** widens
  the `provider_api_keys` CHECK (verified incrementally: 40 → 41 with an
  existing GEMINI key intact). New setting `bedrock_region` (validated as a bare
  AWS region because it lands in a hostname; default `us-east-1`), saved via
  `PUT /api/v1/settings/bedrock-region` and shown in Settings only when BEDROCK
  is selected. `parse_usage` now reads `Converse`'s camelCase
  `inputTokens`/`outputTokens` — without it every Bedrock call was recorded as
  *unreported* usage. ADR-051.
  - Model ids are region-namespaced (`us.anthropic.claude-sonnet-4-6`) and are
    validated against `[A-Za-z0-9._:-]`, not path-escaped, because the id is a
    URL path segment.
  - Bedrock is a reasoning/flash provider; the decision tier still accepts only
    TypeSafe Jev. **Not implemented:** Bedrock tool use (`Converse.toolUse`).
  - Live-verified with a placeholder key: reached AWS, got 403, surfaced as 502
    with no key echoed. A real key is needed for a successful completion.
- **AI Hedge Fund tab (28 Nov 2026):** new **Build → AI Hedge Fund** tab
  (`bq/aihf/`, `web/src/AiHedgeFundPanel.tsx`) over `aihf==2.7.2`
  (`virattt/ai-hedge-fund` v2). It **cannot** share this environment: aihf
  pins `pandas<3`/`numpy<2` and `uv` reports the lock unsatisfiable, its import
  name is `hedge_fund` (not `aihf`), and it has no HTTP API. So it runs from a
  sidecar venv (`just aihf-install` → `bharatquant/.aihf-venv`) as a
  subprocess with a scoped env built from Settings and a private `HOME`. ADR-050.
  - API: `/api/v1/aihf/{status,models,settings,mandates,backtest,backtests,
    paper/*}`; reads need a session, writes need CSRF+origin; SSE progress on
    `/backtest/{id}/stream`. `GET /aihf/settings` is the only projection of the
    Financial Datasets key (masked).
  - **Approval by adoption:** `aihf paper tick` has no dry run, so a preview
    ticks a *copy* of the fund and approval copies aihf's ledger/broker/events
    back. Nothing is re-serialised, so aihf's own hash check re-verifies it.
    Kill switch is checked before preview state; head hash re-checked before
    adoption. Verified: a failing preview left the live fund byte-identical.
  - Key mapping is only what both sides share: `GEMINI→Google`,
    `OPENAI→OpenAI`, `DEEPSEEK→DeepSeek`, `TYPESAFE_JEV→TypeSafe`. No
    Anthropic/xAI/Kimi key exists in this app, so those models are listed but
    not runnable. aihf's registry ids are placeholders — expect a first real
    run to need an id the vendor actually serves.
  - Sidecar is outside `uv.lock`, so `test_dependency_notices` is unaffected.
    `.aihf-venv/` and `aihf/` are gitignored. **Not done:** scheduler job to
    advance funds unattended (`aihf paper run` supervision), and the x402
    keyless data path.
- **Eval timeout 30 s (28 Nov 2026):** `defaultTest.options.timeout` in
  `evals/promptfooconfig.yaml` is now 30000, not 15000. At `num_predict: 512`
  an 8B model needs ~12.6 s here (40.6 tok/s) but a 14B reasoning model needs
  ~20 s (25.8 tok/s), so 15 s truncated the 14B mid-JSON and scored it a
  timeout. `latency_under_10s` is unchanged and still scores the 14B 0 — that
  is the intended comparison, not a bug.
- **Ollama runtime is 0.14.2 (28 Nov 2026):** `gemma4:12b-mlx` and
  `qwen3.5:9b` are stored in the new `tensor`/safetensors manifest format this
  build cannot load — they fail at generation while still reporting
  `installed: true` (the check is name-based off `ollama list`). Registry holds
  four explicit entries: granite4.2:8b (router) and deepseek-r1:14b (reasoning)
  work; the two above need an Ollama upgrade past 0.40, or removal.
- **Local model registry (28 Nov 2026):** open-source models can be added from
  **Model Evals**, pulled with `ollama pull`, benchmarked, and then selected in
  **Settings → Models & keys**. `bq/local_models.py` owns a bounded `LocalModel`
  registry stored in `app_settings.local_models` (no migration); an explicitly
  emptied registry stays empty, and only an absent key seeds the three shipped
  YAML providers. Each eval run generates its own `promptfooconfig.yaml` in the
  run artifact dir (providers block replaced, anchor/assertions kept, dataset
  path absolute) — the tracked `evals/promptfooconfig.yaml` is never mutated.
  API: `GET/POST/DELETE /api/v1/models`, `POST /api/v1/models/pull[/cancel]`;
  `/evals/config`, `/evals/runs` and `/evals/promote` now read the registry.
  Presence uses `ollama list` (`installed: null` = unknown); pulls run one at a
  time with bounded output. Local models route through `CUSTOM`; a keyless
  loopback `custom_endpoint` now counts as configured, and saving a tier derives
  the endpoint from the selected model (one local endpoint at a time). UI: a
  Local models section (add/pull/remove) in Model Evals and a registry-backed
  datalist on the tier model fields. ADR-049.
- **Empty scorecards fixed (28 Nov 2026):** `--filter-providers` is a single
  **regex** in Promptfoo, not a comma list. The runner joined selected ids with
  commas, so any multi-model selection matched no provider, ran zero cases and
  wrote a header-only `scorecard.csv` while the run showed "completed". It now
  passes `^(?:id1|id2|…)$` with each id escaped, and a run with zero case results
  is marked **failed** ("Promptfoo produced no case results") instead of
  completed, so an empty scorecard can no longer look successful. Existing
  empty runs are historical; re-run to get scores.
- **Node preflight for evals (28 Nov 2026):** the runner now picks the first PATH
  `node` that satisfies Promptfoo's engine range (`>=22.22.0`) and prepends its
  directory, so a shadowing `/usr/local/bin/node` (22.17) no longer breaks
  `npx promptfoo@latest`; it fails fast with a clear message if none exists.
- **Agent harness unification (28 Nov 2026):** every agent now shares one
  config-driven harness. `ResearchAgent` routes through `ModelRouter` (so a
  promoted local model in Model Evals actually applies to the Agent Console),
  `research`/`news_research` prompts are editable from the Agent Lab, a new
  `GET /api/v1/agents` catalogue drives one tab per agent, loop-agent runs are
  persisted (`agent_runs`/`agent_run_steps`, migration **040**) with history in
  the Agent Console, and `bq/services.py` centralizes wiring for the scheduler.
  **ADR-048 is implemented:** `ModelRouter.complete` sends any call naming a
  registered `TaskDefinition` through `LLMGateway` (manifest, schema validation,
  citation check, attempt record); generic text keeps the synthetic route path.
  No task definitions are seeded yet, so production behaviour is unchanged until
  one is registered.
- **Model Evals tab (28 Nov 2026):** a new **Build → Model Evals** page benchmarks
  local Apple Silicon LLMs (Gemma 4 12B, Qwen3.5-9B, Granite 4.2 8B) against the
  50-case trading golden set via Promptfoo. Migration **039** adds `eval_runs` +
  `eval_model_scores`; the partial unique index `eval_runs_single_active` is the
  single-run lock (a second `POST` is a 409). `bq/evals/` holds `config.py`
  (stdlib parser for our `evals/promptfooconfig.yaml` + CSV categories),
  `store.py`, `scoring.py` (imports the repo-root `evals/score_promptfoo_results.py`
  by path), and `runner.py` (one sequential `npx promptfoo@latest eval
  --max-concurrency 1` process, `PROMPTFOO_DISABLE_TELEMETRY=1`, SIGTERM cancel,
  scorecard.csv export). API: `/api/v1/evals/{config,runs,runs/{id},
  runs/{id}/cases,runs/{id}/cancel,runs/{id}/scorecard.csv,promote}`.
  The bundle files live in `evals/` at the repo root. Promoting a winning model
  writes `agent_routing` in SQLite and mirrors a readable `config/agent_settings.yaml`
  (projection only; SQLite is truth). A real local endpoint now works:
  `Provider.CUSTOM` may be keyless and `OpenAICompatClient` accepts loopback HTTP.
  - **Live sweep verified (28 Nov 2026):** Node **26.13.1** (Homebrew;
    `/opt/homebrew/bin` prepended in `~/.zshrc` because a root-owned
    `/usr/local/bin/node` v22.17 shadowed it) and **Ollama 0.40.19** (Homebrew).
    `POST /api/v1/evals/runs` → Promptfoo → Ollama generated for real; the run
    `completed` with 20 cases and a persisted scorecard. Three bugs were found
    and fixed only by running it:
    1. `categories` were stored but never passed to Promptfoo → now
       `--filter-metadata category=a,b` (10+10 cases, not 50).
    2. Models rambled past 2,400 tokens per case → `num_predict: 512` on every
       provider (and `failOnError: false` so a slow case aborts, not hangs).
    3. **Exit code 100 means "some tests failed", not a crash** — the runner
       discarded a perfectly good `results.json`. It now scores whenever the
       file exists and only marks `failed` when there is no output.
  - **Full three-model sweep (28 Nov 2026).** All three models pulled (~17 GB in
    `~/.ollama/models`, 30 GB total) and scored through the app, one at a time,
    `earnings` + `noise_filtering` (20 cases each). Two more fixes came only
    from running it:
    - **`think: false` on every provider.** Reasoning generation was most of
      granite's latency *and* the source of the prose. With it off granite went
      **0/20 → 12/20**, p50 **15.3 s → 2.9 s**, JSON parse 0% → 85%.
    - **CUSTOM endpoint path bug.** `OpenAICompatClient` POSTed to `self.endpoint`
      verbatim, so the documented Ollama base `http://127.0.0.1:11434/v1` 404'd
      (needs `/v1/chat/completions`). Promote therefore *looked* fine but could
      never actually call the model. The client now accepts either a base or the
      full completions URL.
  - **Results (20 cases, 10 earnings + 10 noise):**

    | Model | Overall | Task acc | JSON parse | p50 | p95 | Brier ↓ | ECE-5 ↓ | Calib N |
    |---|---|---|---|---|---|---|---|---|
    | Gemma 4 12B (MLX) | 65% | 65% | 100% | 2255 ms | 4262 ms | 0.311 | 0.330 | 20 |
    | Qwen3.5-9B | 60% | 65% | 100% | 2301 ms | 6049 ms | 0.372 | 0.378 | 20 |
    | Granite 4.2 8B | 60% | 60% | 85% | 2942 ms | 13538 ms | 0.375 | 0.456 | 17 |

    Gemma wins on latency, calibration and consistency; it is also the only model
    at **10/10 on `earnings`**. All three score **0.3 on `noise_filtering`**:
    they read the rumour/explainer text correctly (ticker, `event_type: noise`,
    `extracted_numbers: []`) but answer with confidence 0.9–0.95 against the
    0.65 cap — i.e. the filter works and the models are over-confident, which is
    exactly what the Brier/ECE columns exist to show.
  - **Promote verified live:** gemma promoted to `news_research` wrote the SQLite
    route + the `config/agent_settings.yaml` mirror, and the role then resolved
    to `CUSTOM`/`gemma4:12b-mlx` and returned a real completion from Ollama.
- **Operational logging (26 Nov 2026):** `bq/logbook.py` records every LLM call
  (full system/prompt/response, provider/model/tokens/latency/outcome) and every
  model, final-gate, campaign and trigger decision. Entries buffer in
  `<BQ_HOME>/logs/.pending.jsonl` and flush to `bq-YYYY-MM-DD.jsonl` every 15
  minutes (asyncio loop + `logs.flush` job + record-time fallback); retention
  defaults to 14 days and is owner-configurable. Controls and a log viewer live
  in **Settings → Logging**; API `GET/PUT /api/v1/logs` and
  `POST /api/v1/logs/flush`. No migration: settings live in `app_settings.logging`.
- **Catalyst campaign delivered (26 Nov 2026):** the news → catalyst →
  shortlist → deterministic TA/plan → breakout-trigger → paper fill → 5pm review
  loop is wired end to end. Migration **038** adds `equity_master`,
  `news_catalysts`, `campaigns`, `campaign_items`, `campaign_item_events`.
  New modules: `data/equity_master.py` + `infra/equity_listing.py` (bounded NSE
  listing), `catalysts.py` (event taxonomy, polarity, ₹-crore strength, alias
  attribution), `shortlist.py`, `campaign_config.py`, `campaign.py`,
  `triggers.py`, `campaign_feedback.py`, and `analysis/builder.py` (shared
  pipeline builder; `api.py` delegates to it). Yahoo is registered for
  `BARS_INTRADAY`, so delayed 5-minute NSE candles work without Kite.
  - Entry is a breakout above the previous session high with a 0.5% chase cap.
    Plans are swing/CNC, so exits are T+1 (delivery settlement) — the trigger
    engine manages entries and exits from 09:15 to 15:15 every 5 minutes; the
    campaign owns its stop/target (no duplicate `paper_stops` row).
  - Default analysis is `deterministic` (no LLM, zero token cost for 8 names);
    `analysis_depth="full"` opts into the existing debate pipeline.
  - The evening report now carries a "Campaign review — what to change tomorrow"
    section with per-catalyst hit rates and the Bayesian-smoothed weights the
    next shortlist applies.
  - UI: new Daily → **Campaign** tab (pipeline stages, candidates, trigger
    levels, fills, review, settings) and a Today status card. API:
    `/api/v1/campaign/*`, `/api/v1/settings/campaign`,
    `/api/v1/equity-master/*`. Jobs: `campaign.research` 08:05 and
    `campaign.triggers` every 5 minutes.
  - News is multi-source by default: Pulse plus two Google News `when:7d`
    queries for order wins and results. Lookback is 96 hours; publisher suffixes
    are stripped, listicle headlines are skipped, and the longest company name
    wins over a parent ticker. Residual attribution errors are visible in the
    candidate table and are what the 5pm hit-rate table is meant to correct.
  - Caveats surfaced in UI/report: Yahoo candles are ~15 min delayed;
    `calendar_sessions` is empty so session gating uses a weekday/time fallback;
    unmatched corporate headlines are counted.
- **Live paper run (26 Nov 2026, 21:05 IST):** migration 038 applied to the
  owner DB (backup at `~/Backups/bq-pre-campaign-20261126.db`). `paper-main`
  created with ₹10,00,000. Schedules version 1 seeded: `campaign.research`
  08:05, `campaign.triggers` every 5 min 09:15–15:15, plus news/morning/stops/
  square-off/evening/memory. The 27 Nov campaign `camp_20261127` is **ACTIVE**:
  7 names shortlisted, 2 actionable — **LT** breakout above 4160.00 (stop
  4008.88, target 4386.68) and **LCCPROJECT** above 136.63 (stop 121.45, target
  159.40); DBL/NBCC/EMSLIMITED/TEXRAIL/ALEMBICLTD were rejected by the
  deterministic gate. Server is running at `127.0.0.1:8000` (background task
  `s83v01hj`). Next: 08:05 forced rebuild, 09:15–15:15 scans, 17:00 campaign
  review. Run `just run` if the background process is gone.
- Cloudflare tunnel fixed (26 Nov 2026): the panel never left "Waiting for
  Cloudflare" and the tunnel had **never** once reached live (audit DB: 3 ×
  `tunnel.start_failed`, 0 × `tunnel.started`). Two independent causes:
  1. **UI polled once.** `CloudflareTunnelPanel` scheduled a single 1.5 s fetch
     after Start and never rescheduled, so the badge froze on whatever that one
     snapshot said.
  2. **Liveness depended on public DNS.** `cloudflared` created the URL at ~4 s
     and registered its edge connection at ~5 s on `quic`, but the health probe
     called the public `*.trycloudflare.com` hostname. A brand-new name is
     negatively cached by the ISP resolver (measured: system resolver NXDOMAIN
     for ~80 s; Cloudflare DoH resolved in ~16 s), so the probe failed the whole
     `HEALTH_TIMEOUT_SECONDS` window and killed a perfectly good tunnel.
- Fix: the panel polls for as long as the backend reports a busy state, keeps
  the last known status through fetch errors and retries, and leaves Stop usable
  in any non-terminal state. The manager now gates on cloudflared's `Registered
  tunnel connection` line and probes the **loopback origin**, not the public name;
  failures append the last cloudflared `ERR` line; log lines bound at 600 chars;
  `request_start` is a no-op while live. Start/health budgets: 60 s / 20 s.
- Verified end-to-end against real cloudflared: reaches **live in 6–8 s**, poll
  loop terminates, host gate returns **200** for the tunnel host. The owner's own
  browser may still need ~a minute to resolve a brand-new hostname (noted in the
  panel and README).
- Caveat to expect: a `cloudflared` child may outlive a `bq serve` started before
  this fix; restart the server to pick up the backend change.
- Startup fixed (26 Nov 2026): the local dev DB had been initialised from a
  pre-commit draft of `034_reports.sql` (its `prediction_details.kind` CHECK
  lacked `STANCE`), so `bq init`/`doctor` aborted with "Migration history
  differs". Repaired in place; owner password, Gemini key and settings preserved.
  Backup at `~/Library/Application Support/BharatQuant/bq.db.bak-*`. A fresh DB
  applies all 37 migrations cleanly. `_applied_count` now names the first
  divergent migration.
- UI restructured (26 Nov 2026): sidebar groups are Daily (Today,
  Opportunities, Trade), Analyze (Research, Technical, Reports), Build
  (Strategy, Agent Lab), Operate (Data, Schedules) and System (Settings, How to
  use). Data and Schedules are now top-level tabs; Settings narrowed to four
  sub-tabs (Overview, Models & keys, Risk & execution, Runtime & network);
  Today is a read-only command center of linked summary cards. `SchedulesPanel`
  and `OpportunitiesPanel` were previously built but never mounted; both are
  now wired. `PaperAccountStatus.tsx` was removed as redundant. Frontend
  typecheck, 119 tests and the production build pass.
- Single-owner, paper-only Indian equities system. **No broker order path**; Kite
  is read-only. Paper execution is the only execution path.
- Phases 1–19 are implemented: decision/Jev layer, agents, Strategy desk, paper
  execution/realism, memory, confidence, opportunities/shadows, scheduler,
  reports, backtests, research dossiers and runtime hardening.
- **Verification:** 1110 backend tests collected, 1108 pass (the same 2 pre-existing failures
  unchanged), 174 frontend tests; ruff/format clean; frontend typecheck/build
  clean; `bq/local_models.py` and `bq/evals` are mypy-clean; a fresh DB applies
  **41 migrations** (`041` is the Bedrock provider migration). Three real local sweeps and a live promote completed
  end to end (see Model Evals above).
- **mypy:** pre-existing errors remain in `cli.py`, `triggers.py`, `campaign.py`,
  `campaign_feedback.py`, `catalysts.py`, `reports/engine.py`,
  `infra/scheduler.py` and some test fakes. Do not “fix” them as part of
  maintenance; the Model Evals change adds none.
- **Next migration number:** `042` if a new migration is needed.
- **Known pre-existing test failures:** `test_logbook` (filename is hard-coded to a
  date, breaks off that day) and `test_phase5_acceptance` (produces no paper plan).
  Both fail without this change too; do not attribute them to Model Evals.

## Phase 19 delivered

- `bq/runtime.py`: owner runtime snapshot (DB/WAL/free disk, integrity,
  migration count, job leases/stale jobs, table counts), Prometheus text metrics,
  verified online SQLite backup with SHA-256 manifest, guarded restore with a
  `bq.db.pre-restore` copy, and deterministic `bq bench` smoke timings.
- CLI: `bq db backup`, `bq db restore --confirm`, `bq bench`; just targets
  `bench`, `backup` and `restore`.
- API: `GET /api/v1/system/status`, `GET /api/v1/system/metrics` and the
  owner-authenticated `WS /api/v1/events` runtime snapshot feed.
- Runtime card for health, storage, job leases and issues; it now lives in
  Settings → Runtime & network (v1.1), not Today.
- `docs/runtime-operations.md`: launchd/caffeinate, sleep/wake, backup/restore,
  metrics and failure policy.
- Tests: health/stale lease, backup/restore round-trip, metrics, WebSocket
  snapshot, CLI commands and frontend contract.

## Remaining roadmap

The original phase roadmap ends here. There is no Phase 20. Future work is
maintenance/optional breadth:

1. Playwright browser E2E and longer chaos runs.
2. Full XBRL/PDF ingestion and YouTube transcript extraction.
3. Deeper walk-forward optimization and strategy-to-Trade promotion.
4. Automated backup scheduling/retention and external alerting.
5. Live broker integration only with a new explicit ADR; current boundary forbids
   it.

## Invariants

- Paper only; Kite read-only; no broker order path.
- LLMs never compute prices, sizing, P&L, exposure, valuation or thresholds.
- Point-in-time `as_of`; no look-ahead. Predictions frozen before the window.
- Deterministic code owns rules, charges, settlement, stops, scoring, valuation,
  gates and risk.
- Strategy versions, backtest runs, research notes and fundamental snapshots are
  immutable; reviews/events are append-only.
- One lease/fencing job store; run handlers only in the worker.
- Owner auth, origin + CSRF on writes, `no-store` private reads.
- Backups are read-only snapshots; restore requires explicit confirmation.
- Schedule times/windows are configuration, never UI hard-coding.

## Relevant module map

| Area | Files |
|---|---|
| Responsive shell | `web/src/Sidebar.tsx`, `web/src/TableScroll.tsx`, `web/src/styles.css`, `web/tests/responsive.test.mjs` |
| Runtime | `bq/runtime.py`, `bq/cli.py`, `web/src/RuntimePanel.tsx` |
| Tunnel | `bq/infra/tunnel.py`, `web/src/CloudflareTunnelPanel.tsx`, `web/src/tunnelContract.ts` |
| Operations | `docs/runtime-operations.md`, `justfile` |
| API/WS | `bq/api.py` system status/metrics/events |
| Research | `bq/research_expansion.py`, `bq/fundamentals.py` |
| Strategies | `bq/strategy_dsl.py`, `bq/backtest.py` |
| Execution | `bq/paper_trading.py`, `bq/execution/` |
| Campaign | `bq/campaign.py`, `bq/catalysts.py`, `bq/shortlist.py`, `bq/triggers.py`, `bq/campaign_feedback.py`, `bq/data/equity_master.py`, `web/src/CampaignPanel.tsx` |
| Logging | `bq/logbook.py`, `bq/infra/job_handlers.py` (`logs.flush`), `web/src/LoggingPanel.tsx`, `web/src/loggingContract.ts` |
| Model Evals | `bq/evals/` (`config.py`, `store.py`, `scoring.py`, `runner.py`), `evals/*` (bundle), `web/src/ModelEvalsPanel.tsx`, `web/src/evalContract.ts` |
| Local models | `bq/local_models.py` (registry, presence, pull), `bq/settings.py` (`local_models`), `web/src/modelsContract.ts`, Model Evals + `ModelsConfigPanel` |
| AI Hedge Fund | `bq/aihf/engine/` (vendored `hedge_fund`, ADR-053) + `engine/PATCHES.md`, `bq/aihf/bridge/` (`data`, `llm`, `personas`, `clock`, `costs`, `calendar`), `bq/aihf/` (`service`, `routes`, `runner`, `config`, `wiring`, `ingest`, `data_store`, `persona_prompts`), `bq/data/vendors/yahoo_fundamentals_vendor.py`, `web/src/AiHedgeFundPanel.tsx`, `web/src/hedgeFundContract.ts`, `web/src/EquityCurve.tsx` |

## Commands

```sh
cd bharatquant
PYTHONPATH=backend/src uv run --locked pytest -q
PYTHONPATH=backend/src uv run --locked ruff check backend/src/bq tests
PYTHONPATH=backend/src uv run --locked ruff format --check backend/src/bq tests
PYTHONPATH=backend/src uv run --locked mypy backend/src/bq

cd web
npm test && npm run typecheck && npm run build

# operations
BQ_HOME="$HOME/Library/Application Support/BharatQuant" uv run --locked bq bench
BQ_HOME="$HOME/Library/Application Support/BharatQuant" uv run --locked bq db backup --output "$HOME/Backups/bq.db"

# operational logs (Settings -> Logging flushes manually; the loop runs every 15 min)
tail -n 5 "$HOME/Library/Application Support/BharatQuant/logs"/bq-*.jsonl

# ai hedge fund: vendored in-process (ADR-053), nothing to install.
# Fundamentals are populated by the `aihf.fundamentals.ingest` job — enable it
# on Schedules (off by default: one Yahoo request per symbol).

# campaign (first run creates paper-main, saves defaults, seeds schedules)
uv run --locked bq campaign init
uv run --locked bq campaign refresh-master
uv run --locked bq campaign research
uv run --locked bq campaign scan
uv run --locked bq campaign status
uv run --locked bq campaign review
```
