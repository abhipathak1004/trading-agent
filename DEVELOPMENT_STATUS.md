# BharatQuant development status

Start here when resuming work. This file lives in the workspace and does not depend on chat history or session todos.

## Current handoff

Updated 26 November 2026. The roadmap is **not complete**: the application is
an authenticated offline foundation, not an operational paper-trading system.

**Latest slice — Phase 1+2: config-driven three-tier routing and the Jev decision battery.**
Analysis runs can now route each agent to a decision, reasoning or flash tier from Settings, with no code edit. The decision tier prefers TypeSafe Jev and degrades through the configured LLM tiers to a deterministic heuristic and, finally, `RULES_ONLY`.

- **Reference studied:** `buberlo/jev-trader` (MIT, surveyed 2026-11-26) — deterministic dense state → one atomic Choice/Noul/Score battery → code-owned policy → hard risk vetoes → calibration triples. Architecture reused, original code only (ADR-018).
- `bq/decision/` — `types.py`, `state.py`, `battery.py`, `client.py`, `fallback.py`, `policy.py`, `calibration.py`. State is dense, rounded, `None`-free and bounded (16 KiB / ~2k tokens); batteries are atomic and independent; Jev pins the model, records the returned version, request id and latency, flags `model_mismatch`, and never retries past its deadline; `HeuristicJudge` is deterministic with fixed 0.5 confidence.
- `bq/model_router.py` — per-agent tier resolution, `RoutedModelCaller` for the existing `Debate`/`RiskAnalyst`, and the ladder Jev → Flash → Reasoning → heuristic → `RULES_ONLY`. `RouteUse` records the exact source/model/latency/state hash in `analysis_runs.detail_json`; trace summaries show fallbacks.
- Settings: new tier keys (`tier.*`), `agent_routing`, `decision_route`, `decision_policy.*`, `pipeline.*`; defaults keep the legacy `provider`/`model_name` path working. Migration `026_tier_providers.sql` admits `TYPESAFE_JEV` to `provider_api_keys` (table rebuilt; keys preserved). New `PUT /api/v1/settings/models`; `bq settings show` prints tiers, routing, policy and the decision preview.
- UI: **Settings → Model tiers, decision policy & routing** — three tier cards, fallback order, thresholds, agent→tier table, and a live "Decision will run on: Jev / Flash / Reasoning / Heuristic / Rules-only" line. `parseModelsConfig` bounds every field.
- Jev is optional by construction: no key, a 4xx/5xx/timeout, or an invalid/oversized answer falls through to the configured tiers; with no keys the run completes deterministically. `decision_source` and the returned model will bucket Brier/ECE calibration in Phase 3.
- Docs: `docs/ARCHITECTURE_PLAN.md`, ADR-016/017/018, TypeSafe review in `docs/external-source-gates.md` (endpoint/primitives/errors checked; terms/privacy pending).

Verification: **728 backend tests** pass (51 new: 48 decision-layer + 3 settings API), **46 frontend tests** pass (5 new), ruff and format clean, frontend typecheck + build clean, `bq init`/`bq doctor` OK on a fresh temp database (26 migrations). mypy reports the same 25 pre-existing errors in untouched `cli.py`, `paper_performance.py` and `infra/scheduler.py`; the new modules are clean.

Next: Phase 3 — wire the news agent into `_news_context`, add the Fundamental/Events and Market Regime specialists, the four-layer memory (`episodic_memory`, FTS5 + fallback), shadow trades and source/model-bucketed Brier, then evolve `judge` into the Super Agent and add the Jev final gate.

**Latest slice — Technical Analysis tab: multi-symbol, multi-agent, from the UI.**
A new **Technical** tab submits one or more stocks and shows every agent's
contribution. This makes the existing multi-agent framework usable from the UI;
it is not a new analysis engine.

- `bq/analysis/batch.py` — `BatchAnalyst` fans the existing pipeline over up to
  10 symbols. One symbol's failure never stops the batch (a mistyped ticker is
  reported against that symbol), duplicates are collapsed before any vendor call,
  and results come back in the requested order. `parse_symbol_list` owns the
  parsing rules server-side (commas, semicolons, newlines; case-insensitive
  de-dupe) so the browser does not re-implement them.
- `bq/analysis/pipeline.py` — added `AgentTrace`, so each run reports what every
  agent did: `Market data`, `News context`, `Technical analyst`, `Bull advocate`,
  `Bear advocate`, `Research judge`, `Risk analyst`, `Position sizer`, `Decision`.
  Each carries a status (`ok` / `degraded` / `failed`) and a one-line summary.
  Entries are ordered as the pipeline reads, not as the DAG sorted them (which
  had put bear before bull).
- API: `POST /api/v1/analysis/batch`. Same owner-only, `no-store`, CSRF + origin
  checks as the single-symbol route; the two now share one `_analysis_pipeline`
  builder so they cannot drift. Each successful analysis is also persisted to the
  run history.
- UI: new `TechnicalAnalysisPanel.tsx` and a **Technical** nav item. Enter
  stocks, pick a timeframe, set equity/cash, run. Each symbol card shows the
  signal, four sub-scores, the plan, and an expandable agent trace. A degraded
  agent is shown as degraded rather than hidden — "the debate did not run" is
  information the owner needs.

**Fix — "Invalid analysis run" on the Technical tab (found in manual testing).**
The batch route returned the pipeline's raw `to_dict()`, which has no `run_id`;
only `AnalysisRunStore` mints one. The UI contract required `run_id`, so every
real batch was rejected with *Invalid analysis run*. The frontend test passed
because its fixture reused an object that happened to carry the field — the
fixture did not match the real payload.

- The API now attaches `run_id` from the store record it already writes, so a
  batch item has the same shape as a single-run response.
- `parseAnalysisRun` treats `run_id` as **optional metadata**: a payload without
  one still renders, so a future shape drift degrades gracefully instead of
  blanking the whole result.
- Regression tests on both sides: the frontend now parses a payload with the
  field *removed*, and a backend test asserts every contract key is present on a
  successful batch item (with data access stubbed). One existing test that
  asserted "a missing `run_id` is invalid" was encoding the bug and was corrected.
- Verified against the real API: the live response parses through the actual
  browser contract (`succeeded 1 / failed 1`, 9 agents, `run_id` present), not
  just through a fixture.

Live verification: a batch of `NSE:RELIANCE, NSE:TCS, NSE:HDFCBANK, BOGUS`
returned `3 succeeded / 1 failed`, with `BOGUS` failing cleanly as
`No market data for NSE:BOGUS` while the other three produced full plans. The
trace for RELIANCE showed all nine agents in pipeline order: `247 bars from
yahoo (delayed)` → `4/4 dimensions scored, composite +0.23` → `2187 shares, R:R
1.50` → `BUY`, with the four LLM agents correctly marked `degraded` because no
model key is configured.

Verification: **677 backend tests** pass (30 new: 17 batch, 12 batch-API, 1
ordering), **41 frontend tests** pass (10 new), ruff clean, format clean, frontend
typecheck + build clean. mypy stays at the 47 pre-existing errors in the same 8
untouched files.

**Previous slice — repository cleanup and deduplication (no behaviour change).**
Removed dead code, consolidated copy-pasted helpers, and stopped tracking
generated files. Verified: **648 backend tests** (647 + 1 new), **31 frontend
tests**, ruff clean, format clean, mypy unchanged at 47 pre-existing errors in 8
files, frontend typecheck and build clean, `bq doctor` OK.

- **Repo hygiene.** `git ls-files` went from **4,250 to 283** files:
  `bharatquant/web/node_modules` (3,970 files, already listed in `.gitignore` but
  force-added), the tracked `bq.db`, `pinak.zip` and two `.DS_Store` files are now
  untracked and ignored. *No secret was in the tracked database — it stopped at
  migration 17, before `provider_api_keys` existed — but it has been committed and
  `dev` tracks `origin/dev`, so it is in remote history. It also contained the
  owner Argon2 hash and a hashed session token. Rotating the owner password is
  cheap insurance if that remote is ever shared.*
- **New `bq/core.py` helpers** replace copies across 22 modules: `utc`,
  `timestamp`, `parse_timestamp` (was 20 copies), `open_connection` (21),
  `canonical_digest` (6), `IDENTIFIER` (10 identical declarations) and
  `paise_from_price` (2). Every module keeps a thin `_utc`/`_timestamp` wrapper
  so its own error class and message are preserved, and every store keeps a
  `_connect` method because tests monkeypatch it.
- **`open_connection` keeps each store's existing PRAGMA policy.** The 6 stores
  that never enabled `foreign_keys` still pass `foreign_keys=False`; turning
  enforcement on is a separate change with its own risk.
- **One behaviour tightened:** `PaperExecutor._price_paise` now shares
  `paise_from_price`, so a non-positive price is rejected instead of silently
  converted. Unreachable from a validated `TradePlan`; asserted by a new test.
- **Real duplication removed:** the Yahoo bounded-streaming fetch existed twice
  (`data/yahoo.fetch_chart` and `yahoo_vendor._fetch_window`); both now share
  `yahoo.stream_chart`, so `fetch_chart` gains the vendor's typed HTTP mapping.
  The paper ledger DEBIT/CREDIT aggregate is now one `LEDGER_TOTALS_SQL`
  constant. `MAX_SYMBOL_CHARS` is declared once.
- **Frontend:** new `web/src/contracts.ts` holds `isRecord` / `isCount` /
  `isTimestamp` / `formatUtc`, replacing 15 byte-identical copies across 5
  contract modules and 4 panels. Removed an unused `onSaved` prop, an unused
  `.signed-in` CSS rule, and two dead exports; `UsageStatus` now renders the
  `provider_connected` value it was already parsing.
- **Bundle:** `just build` regenerated `static/assets`; `index.html` and the
  files on disk agree.

Deliberately **not** touched: the 19 modules unreachable from `cli.py`/`api.py`
(~3,010 lines — predictions, opportunities, strategies, replay, research,
universe, confidence, benchmarks, macos, llm_tokens, llm_gateway, model_output,
risk/decisions, reports/scoring, data/providers, data/context,
infra/context_routing). They are documented P2–P9 foundations with passing tests.
Also left alone, after checking: the news / paper-account / context layering,
`data/symbols` vs `universe`, the risk trio, and the two ticker validators
(they enforce different, deliberate policies).

**Latest slice — Phase 4: the deterministic risk gate is wired to paper execution.**
A validated `TradePlan` now runs through the existing pre-trade gate and, only on
`ALLOW`, becomes a **paper** order. Nothing touches a broker; ADR-001 stands.

- `bq/execution/paper_executor.py` — `PaperExecutor`. It maps data freshness to
  the gate's `data_usable` flag (delayed → `WARN` and still usable for paper;
  critically stale → `BLOCK`), calls `evaluate_pretrade` with every limit, and
  places a paper order only when the decision is `ALLOW`. It refuses `SELL`/
  `HOLD` plans (no short-margin model) and relays the plan's quantity unchanged.
  **Swing freshness counts weekday sessions missed, not calendar seconds**, and
  measures from the bar's close rather than its open stamp; a calendar threshold
  had blocked every Monday for a plan built on Friday's close (ADR-015).
- `bq/analysis/decision.py` — added `trade_plan_from_json`, a re-validating
  boundary so a persisted or hand-edited plan is checked on the way back in. An
  invalid enum field now names the offending JSON field instead of saying
  "invalid".
- CLI: `bq paper execute <plan.json|-> --account-id … --session-open …` plus
  explicit `--max-*` risk limits (there are **no** invented defaults for risk).
  `just paper-execute`. `bq doctor` no longer claims market data and paper
  trading are unimplemented.

Live verification: `bq analyze NSE:RELIANCE` produced a real plan (449 shares,
entry 1351.00 / stop 1306.47 / target 1417.80, R:R 1.50), and executing it against
a funded paper account returned `ALLOW`, `delayed_warn`, and placed a 449-share
LIMIT paper order at 135100 paise.
Testing also confirmed the negative case: `NSE:TCS` scored a negative composite
and correctly produced `HOLD` with `no_deterministic_long_case` instead of a
forced BUY.

Verification: **647 backend tests** pass (31 new), **31 frontend tests** pass,
ruff clean, format clean. mypy is unchanged at 47 pre-existing errors in the same
8 untouched files.

**Previous slice — Indian market-data layer + technical analysis agents.**
A dual-vendor data layer (Zerodha Kite read-only + Yahoo) behind a per-domain
router, plus a deterministic technical analyst, a bull/bear debate, and a
validated trade-plan contract. Stages 1–3 of
[the porting plan](./bharatquant/docs/migration-tradingagents.md).

- `bq/data/symbols.py` — `SymbolResolver`. One canonical identity
  (`NSE:RELIANCE`) mapped to Yahoo (`.NS`/`.BO`, indices as `^NSEI`/`^NSEBANK`/
  `^BSESN`/`^INDIAVIX`) and Kite (`("NSE", "RELIANCE")`). Indices resolve but are
  marked non-tradeable.
- `bq/data/vendors/` — the market-data layer. `base.py` defines the `Bar`/
  `Quote`/`MarketDataVendor` contract; `router.py` holds ordered per-domain chains
  with a public `register_vendor(domain, vendor, priority)` hook (adding a vendor
  needs no core edit); `errors.py` distinguishes `VendorUnavailableError` (try the
  next vendor) from `NoMarketDataError` (there is genuinely no data).
  `yahoo_vendor.py` wraps the existing bounded Yahoo parser and adds an intraday
  window; `kite_vendor.py` is **read-only** — quote, ohlc, historical_data,
  instruments, and nothing else.
- `bq/data/instrument_master.py` — per-trading-day Kite instrument cache with
  equity-only filtering. A failed refresh leaves the prior day readable and
  raises, rather than silently serving stale tokens.
- `bq/data/broker_credentials.py` — Kite credentials in their own table,
  separate from `provider_api_keys`, following the existing masked-secret rules.
  `kite_enabled` defaults **off**; with it off the chain is Yahoo-only.
- `bq/compute/indicators.py` — pandas/numpy indicators (SMA, EMA, RSI-Wilder,
  MACD, Bollinger, ATR-Wilder, VWAP, ADX, drawdown, volume, swing levels).
  Explicit warm-up: an unfilled window returns `None`, never a seeded number.
- `bq/analysis/` — `technical.py` (four bounded sub-scores with stated
  evidence), `debate.py` (bull/bear + judge, where the judge's confidence is
  **clamped** to a deterministic ceiling and can never exceed it), `risk_team.py`
  (advisory only — it cannot veto), `decision.py` (a `TradePlan` that rejects any
  quantity the sizer did not produce), `pipeline.py` (the DAG, reusing
  `bq/agents.py`), `run_settings.py` (append-only run audit), `wiring.py`.
- Interfaces: owner-only, `no-store` `GET /api/v1/analysis/vendors`,
  `POST /api/v1/analysis/run`, `GET /api/v1/analysis/runs[/{id}]`; CLI
  `bq analyze NSE:RELIANCE --timeframe swing --equity-paise ... --cash-paise ...`
  and `bq data vendors`; `just analyze`, `just vendors`.
- UI: new `AnalysisPanel.tsx` on the Research tab, showing sub-scores, the
  fact list, the plan and the data-provenance line. `analysisContract.ts` parses
  and bounds every response, and refuses a non-`LIMIT` plan.

**Kite is a data vendor, not a broker.** ADR-013 reaffirms ADR-001: no order
placement, no sandbox, no `LIVE_AUTO`. A test asserts `kite_vendor.py` exposes no
order-placing method.

Live verification on this Mac: `bq analyze NSE:RELIANCE --timeframe swing`
fetched real Yahoo data, computed all four dimensions over 246 daily bars, and
produced `entry 1351.00 / stop 1306.47 / target 1417.80`, `R:R 1.50`, 224 shares,
labelled `delayed_yfinance` and `RULES_ONLY` (no model key configured). Two real
bugs were found and fixed by that run: prices carried 16 decimals, and
support/resistance could be reported *inverted* (price outside its own range).

Verification: **616 backend tests** pass (114 new), **31 frontend tests** pass
(8 new), ruff clean, frontend typecheck + build clean. mypy reports the same 47
pre-existing errors in 8 untouched files; every new module is clean.

**Previous slice — the research agent is live and playable in the UI.**
A real LLM drives a bounded tool loop end to end; the Research tab is now an
interactive console that shows every tool call.

- `bq/research_agent.py` — `ResearchAgent` wires `bq.tools` +
  `bq.agent_runtime` + `bq.llm_service`. It renders the running conversation
  into one prompt with a strict JSON tool-call contract, calls the configured
  provider, and runs until `response` or the turn limit. Returns a step trace
  and real token totals across all calls.
- `bq/tools/market.py` — `stock_quote` (latest close/change from Yahoo),
  `stock_download` (persist PIT bars), and `web_fetch` (HTTPS page to bounded,
  script/style-stripped text). All bounded and transport-injectable.
- `bq/agent_runtime.py` — the loop now records `AgentStep`s (turn, tool, args,
  previewed output) so the UI can show the trace; tool-call parsing uses the
  vendored dirty-JSON recovery.
- API: `GET /api/v1/agent/status`, `POST /api/v1/agent/run` (agent run with
  step trace), and `/api/v1/tools/research` now reflects the full research
  toolset. All owner-only, `no-store`, CSRF + origin checked.
- UI: new `AgentConsole.tsx` on the Research tab — chat thread, suggestions,
  tool list, per-answer step trace, token/meta line, and a clear "configure a
  model" state when no key is set. `newsContract`/`agentContract` parse and
  bound every response.

Live verification on this Mac with the owner's Gemini key
(`gemini-3.5-flash-lite`): "Quote RELIANCE.NS" → the model called
`stock_quote`, read real Yahoo data, and answered "close INR 1351.00, down
-1.92%"; "summarise long-term news" → called `news_memory` and produced a
grounded 2-bullet summary. A transient Gemini 503 was surfaced as a clean
typed error and a retry succeeded — retry logic behaved correctly.

Verification: **402 backend tests** pass (12 new: research-agent loop with a
fake model, market/web tools with mocked transports), 23 frontend tests pass,
ruff clean, mypy clean on new modules, frontend builds.


**Latest slice — LLM-calling layer hardened with Agent Zero helpers (MIT).**
Real model calls now go through a richer, retrying stack. Self-contained
helpers were adapted from Agent Zero; the LiteLLM transport and LangChain
`call_llm` were **not** ported because they bind to dependencies we don't use
(ADR-012).

- `bq/llm_result.py` — `LLMResult`/`LLMUsage`/`FunctionCall`; handles both
  Gemini and OpenAI usage shapes; `truncated` flags a cut-off answer.
- `bq/llm_extract.py` — separates `<think>`/`<reasoning>` from visible text,
  normalises OpenAI + Gemini tool calls, maps finish reasons.
- `bq/llm_retry.py` — transient-vs-permanent classification for `httpx`,
  capped exponential backoff, and a sliding-window `RateLimiter`.
- `bq/llm_tokens.py` — dependency-free token estimation/trimming (no
  `tiktoken`; documented approximation for budgeting only).
- `bq/dirty_json.py` — **vendored verbatim** from Agent Zero (MIT), used only
  via `parse_tool_call(recover=True)`; strict parsing stays the default.
- `bq/llm_client.py` — both clients now return `LLMResult` and retry
  transient failures; `llm_service` returns `CompletionOutcome` exposing
  text, reasoning, finish reason and tokens. The API/CLI surface reasoning
  and a `truncated` flag.

Live check on this host: Gemini and DeepSeek endpoints answered; bogus keys
produced clean typed errors (HTTP 400 / 401, no retry, no secret leak).

Verification: **390 backend tests** pass (16 new in `tests/test_llm_ported.py`),
18 frontend tests, ruff clean, mypy clean on the new modules, runtime-dependency
guard still passes (no new runtime deps). Attribution in each file header,
`THIRD_PARTY_NOTICES.md`, and ADR-012.


**Latest slice — live LLM calls + live stock data (provider gate opened).**
The "no provider connected / providers disabled" dead end is removed. An
owner who configures a provider, model name and key in Settings now gets a
real route.

- `bq/llm_client.py` — `GeminiClient` (Generative Language REST) and
  `DeepSeekClient` (OpenAI-compatible; also serves `OPENAI`/`CUSTOM`). HTTPS
  only, bounded response size, hard timeout, injectable transport so tests
  never hit the network. Errors are typed and never echo secrets.
- `bq/llm_service.py` — `candidate_for(config)` builds an eligible
  `ModelCandidate` from the configured provider/model/key; `LLMService.complete`
  runs the existing routing gate (satisfied from config, not bypassed) then
  calls the provider. Raises `LLMNotConfigured` when key/model are missing.
- `LLMGateway` now derives candidates from Settings when none are passed, so
  `prepare()` routes to the configured model instead of always failing.
- `bq/data/yahoo.py` — live EOD bars from Yahoo Finance
  (`query1.finance.yahoo.com`), verified live. Drops null/incomplete rows,
  stores `EOD_BAR` records `UNASSESSED` with an idempotent import key.
- Interfaces: `POST /api/v1/llm/complete`, `POST /api/v1/data/download`, and
  `bq llm ask "..."`, `bq data download RELIANCE.NS --range 5d`.
- Status surfaces updated: `/api/v1/data/status.live_provider` and
  `/api/v1/llm/usage.provider_connected` now reflect configuration instead of
  hardcoded "not_configured"/false.

Live verification on this host (network reachable): Yahoo chart download of
`RELIANCE.NS` stored 4 real EOD bars; the LLM path fails cleanly with
"No model name is configured." when unset. The ``llm ask`` path is exercised
end-to-end with an injected fake client (no real key on this machine).

Honesty notes: Yahoo is not an authoritative exchange feed; bars are stored
`UNASSESSED` and must not back a readiness gate yet. The owner-supplied key is
treated as "terms reviewed / capability verified" by assertion, not vendor
certification. Trading stays paper-only; nothing here places orders.

Verification: **374 backend tests** pass (9 LLM + 7 Yahoo new), 18 frontend
tests pass, ruff clean, mypy clean on the new modules, frontend builds.


**Latest slice — agent tool layer (adapted from Agent Zero).** The news
research agent now exposes a reusable **tool layer** under `bq/tools/`.
Structure is adapted from Agent Zero (a `Tool` base returning a message plus a
`break_loop` flag, tool *policy* separate from discovery, a `response`
terminator, an `unknown` fallback); the code is original and reimplemented to
BharatQuant conventions — **no files were copied**, per the plan's rule against
importing external source or prompts.

- `bq/tools/base.py` — `Tool`/`ToolSpec`/`ToolArgument`/`ToolResult`,
  bounded argument + result validation, and `render_tools_prompt`.
- `bq/tools/registry.py` — explicit `ToolRegistry` (no import-by-path, so a
  misconfigured directory cannot execute code) plus `AgentToolPolicy` and
  `ToolRunner`. A `read_only` profile refuses every `mutating` tool; `blocked`
  names refine it. This is the mechanism the plan needs so research agents
  cannot place orders.
- `bq/tools/parsing.py` — a **strict** tool-call extractor (whole response,
  one fenced ```json block, or the first balanced `{...}`). Values are never
  repaired; malformed calls fail with a typed error. Deliberately stricter
  than Agent Zero's forgiving extractor.
- `bq/tools/control.py` — `response` (ends the loop) and `unknown`
  (returns the available tool names).
- `bq/tools/news.py` — `news_search`, `news_memory`, `news_digest` (read-only)
  and `news_ingest` (mutating). `news_memory` records `hit_count` for later
  frecency pruning.
- `bq/agent_runtime.py` — a deterministic loop over an injected `ModelTurn`.
  **It calls no provider.** History is bounded (40 entries) with a rolling
  summary folded deterministically, no summarising model call — the Agent Zero
  history idea without the LLM. Exhausted turns or a `break_loop` both end it.
- `NewsResearchAgent.tools()`, `.tool_registry()`, `.runner(read_only=...)`
  expose the toolset; the research agent allows mutation, a read-only profile
  hides `news_ingest`.
- Interfaces: `GET /api/v1/tools/research` (catalog),
  `POST /api/v1/tools/research/run` (owner-only, CSRF + origin), and
  `bq tools list` / `bq tools run NAME --args '{"query":"nifty"}'`.

Verification: **358 backend tests** pass (25 new in `tests/test_tools.py`),
ruff clean on changed files, mypy clean on the new modules (the 16 pre-existing
errors elsewhere are untouched), and the runtime-dependency guard still passes.


**Latest slice — Research tab + editable Settings.** The UI now has a
**Research** tab and a writable **Settings** page.

- **Settings (writable config)** — `bq/settings.py` (already committed) is now
  wired into the API and UI. Migration `023_application_settings.sql` adds
  `app_settings` (non-secret) and `provider_api_keys` (secrets). New endpoints
  (all owner-only, `no-store`, CSRF + origin checked on writes):
  `GET/PUT /api/v1/settings/config`, `PUT/DELETE
  /api/v1/settings/api-keys/{provider}`. Configuration covers provider, model
  name, news feed URL and long-term-memory retention (days). API keys are never
  returned to the browser — only a masked hint (`********abcd`); the real key is
  available internally via `SettingsStore.api_key`. Keys are stored in
  plaintext in the local DB (acceptable for a single-owner loopback app, not
  vault-grade). Every change appends an audit event without secret material.
  `model_route_configured` is true only when a model name *and* the matching
  provider key exist. **No model provider is called by this build.**
- **Research tab** — new `ResearchPanel.tsx`: reads `GET /api/v1/news/digest`
  and runs `POST /api/v1/news/ingest` (CSRF + origin). Ingest honours the
  configured feed URL and retention via `NewsResearchAgent.run(memory_ttl_days=
  ...)`. Still deterministic: zero model calls, no full-article fetches.
- **Settings overview** — `/api/v1/settings/overview` now reports real
  `provider_connected`, `api_keys_configured`, `agent_configuration_state` and
  an embedded `config` block.
- **CLI parity** — `bq settings show` prints masked config as JSON; `bq news
  ingest` now uses the configured feed URL and retention (pass `--source-url`
  to override).

Verification: **341 backend tests** pass, **18 frontend contract tests** pass,
ruff lint clean on changed files, frontend typecheck + build clean. 16 mypy
errors remain in files untouched here (`cli.py` EOD-report union,
`paper_performance`, `infra/scheduler`, `test_agents`, `test_llm_gateway`) and
predate this work.

The React shell uses the companion UI/UX guide's dark app-shell pattern:
fixed navigation, a top status strip, a clear paper-only mode banner, and
responsive owner setup/dashboard states. Navigation covers Today, Research and
Settings; Trade and Agent Lab remain marked unavailable.
The latest additions are `infra/dispatch.py` and `infra/scheduler.py`: a
wake-to-job dispatch layer and a small 30-second asyncio process loop. The
`bq serve` lifespan starts the loop and reports its running state in schedule
status. It records due decisions and enqueues matching `JobRun` occurrences,
but still does not execute job handlers or provide schedule-editing UI.

The web shell is re-themed to a dark violet accent (`#0B0812` background,
`#8B5CF6` accent, Inter + JetBrains Mono self-hosted under `web/public/fonts/`).
Fonts are bundled via `@font-face` from the same origin, matching the existing
`font-src 'self'` CSP. The owner setup flow now signs the owner in immediately
(`POST /api/v1/auth/setup` returns `signed_in` + session cookie + CSRF token on
success), so the first visit no longer requires a separate sign-in form.

The checked-in frontend bundle is generated from `bharatquant/web`. After
changing the frontend, run `npm run build` in `bharatquant/web` so the packaged
SPA stays synchronized with its source.

## Latest checkpoint — development skills

Added repository-scoped Copilot instructions and focused Clean Software
Craftsmanship, Graphify, and Ponytail skills under `.github/`. The project
README documents how contributors should apply them. Clean Software
Craftsmanship's existing full source remains canonical. The project guidance
does not install Ponytail's plugin or slash commands, and neither tool is a
BharatQuant runtime dependency.

Graphify is an optional local developer tool. Keep extraction local and
code-only, and verify graph findings against source and tests. A baseline
`graphify-out/` (1,490 nodes, 5,277 edges, 47 communities) was generated
code-only on 25 November 2026. ``graphify-out/`` remains gitignored and is a
local cache, not a deliverable.

## Scope and decisions

- Requirements: [trading agent plan.md](./trading%20agent%20plan.md). The [approved implementation plan](<C:/Users/apatha08/.copilot/session-state/6ccb5309-4c8d-4c32-a97f-865ae2c68ffa/plan.md>) is the more detailed work breakdown; if the session file is unavailable, use the source and decisions below.
- Personal Apple Silicon Mac, one owner, paper trading only. Seed `AUTO` paper approval, but never bypass deterministic risk, promotion, reconciliation or kill controls.
- Schedules and runtime windows are editable. Default morning/evening jobs at 08:00/17:00 IST; keep-awake defaults to 07:45–17:30. Never backdate missed work.
- Delayed intraday data defaults to `WARN`; critically stale or invalid data still blocks new entries. Unavailable LLM confidence review permits labeled `RULES_ONLY` after deterministic gates pass. A known veto still stands.
- Verify current exchange rules, terms, licenses and platform capabilities before enabling dependent features. Prefer vetted dependencies and original code/prompts; do not copy external source or prompts.
- Apply [Clean Software Craftsmanship](./clean-software-craftsmanship/SKILL.md): small verifiable increments, domain policy independent of infrastructure, clear failures at boundaries, tests for the risk being changed.
- Apply the repository's [Copilot skill instructions](./.github/copilot-instructions.md): use the relevant Clean Software Craftsmanship, Graphify, and Ponytail guidance on future development tasks. Keep Graphify local and code-only; verify graph findings against source and tests.

## Deferred external-access checklist

Complete these tasks last on a separate machine with authorized access. Do not
bypass this machine's network policy. Record the source, retrieval date,
effective date, applicability and review outcome before enabling the affected
features.

- [ ] Review [NSE market timings](https://www.nseindia.com/static/market-data/market-timings), special sessions and the applicable holiday calendar; record date-specific rules and owner review before enabling session-dependent work. Requests timed out here; a firewall cause is not confirmed.
- [ ] Review official NSE market-data licensing, distribution and permitted-use terms, plus candidate EOD/intraday source capabilities and permissions. NSE requests timed out here; do not activate an adapter based on an unverified claim.
- [ ] Review official Gemini API terms, privacy, eligible models, pricing and quota/reset behavior. Access was denied by this machine's network policy; do not enable Gemini or assume free-tier privacy.
- [ ] Review official DeepSeek API terms, privacy, eligible models, pricing, quotas and usage reporting. The documentation returned HTTP 403 here; the cause is not established.
- [ ] After the above reviews, record approved sources and effective dates in [external-source-gates.md](./bharatquant/docs/external-source-gates.md), complete authorized provider contract/smoke tests with owner-supplied credentials, and enable only the capabilities supported by the evidence and applicable policy.

Separate from network access, GitHub CI, project-license decisions and testing
on the owner's Apple Silicon Mac remain open; see the checkpoint below.

## Work packages

| ID | Status | Deliverable |
| --- | --- | --- |
| P0 | In progress | Foundation: backend/CLI, web shell, security, CI and external verification |
| P1 | Complete (offline) | Seedless calendar, point-in-time data, offline CSV import, durable jobs, dependency-aware daily/interval schedules, paper accounts, exact-paise OMS, fills/positions reconciliation, TWR/XIRR primitives and caller-supplied point-in-time universe publishing; live adapters, verified market facts and live/broker behavior remain excluded |
| P2 | In progress | Offline context, task-definition registry, eligibility checks, attempt ledger and provider-blocking gateway; private-provider evidence, actual clients and full privacy controls remain incomplete |
| P3 | In progress | Binary scoring plus immutable frozen predictions/resolutions and append-only outcome audit; reports, archive and evidence-backed coverage remain incomplete |
| P4 | In progress | Deterministic evidence/confidence gate and immutable opportunity/shadow tracking; chart/volume provider evidence and live calibration remain incomplete |
| P5 | In progress | Fixed-fractional sizing plus deterministic risk/approval gates and immutable decisions; authoritative accounts are offline, execution and accounting remain incomplete |
| P6 | In progress | Strategy lifecycle, replay contamination guard and point-in-time replay cursor; backtest engines and strategy promotion persistence remain incomplete |
| P7 | In progress | Typed in-process DAG runner with optional-node and risk/execution invariants; node persistence and asynchronous execution remain incomplete |
| P8 | In progress | Filings and thesis data primitives with point-in-time publication/ingestion ordering; valuation and filing evidence remain incomplete |
| P9 | In progress | macOS sleep-gap detection, launchd plist generation and benchmark regression contract; target-Mac UAT/backups remain incomplete |

## Latest checkpoint — P2 through P9 offline foundations

The development handoff now includes verified offline foundations for the
authoritative model gateway, immutable predictions and outcomes, deterministic
confidence/opportunities, persisted risk/approval decisions, strategy replay
guards, typed DAG orchestration, filing/thesis records and macOS sleep/launchd
benchmark primitives. The full backend suite passes 301 tests under the available
Python 3.12 environment; Python 3.13, Node 22, Apple Silicon UAT and external
provider verification remain target-environment gates. Live clients and market
adapters remain disabled.

## Latest checkpoint — wake-to-job dispatch

Added `infra/dispatch.py` and `tests/test_dispatch.py`. `dispatch_one` converts
a single `WakeDecisionRecord` with `WakeDecision.RUN` into a durable `JobRun`
occurrence by looking up the effective schedule at the occurrence time, deriving
a canonical occurrence key (`<schedule_id>:<scheduled_at_iso>`), and calling
`JobRunStore.enqueue` with the schedule's `job_id` and `max_attempts`. Non-RUN
decisions are silently skipped. `JobRunConflict` on parameter mismatch is
surfaced as an error; matching parameters are idempotent. `dispatch_due` applies
`dispatch_one` to a batch, continuing on errors. No automatic trigger, worker
loop, dependency ordering or catch-up safety is implemented.

Implemented in [bharatquant](./bharatquant/):

- Python 3.13 package and CLI (`bq init`, `bq doctor`, `bq db migrate`, `bq calendar record-session`, `bq data import-eod-csv`, `bq data inspect-eod`, `bq data inspect-symbol`, `bq data inspect-snapshot`, `bq llm usage`, `bq serve`), with [locked dependencies](./bharatquant/uv.lock).
- [SQLite bootstrap and migrations](./bharatquant/backend/src/bq/infra/database.py): WAL, transactional SQL migrations, version/checksum tracking, integrity check and audit-chain verification, and refusal to initialize a foreign database.
- [FastAPI health endpoint and static SPA serving](./bharatquant/backend/src/bq/api.py) at `/api/v1/system/health`; `bq serve` binds to `127.0.0.1:8000`. Unknown API GET paths stay 404 rather than returning the SPA.
- [Owner authentication](./bharatquant/backend/src/bq/auth.py), [additive authentication migration](./bharatquant/backend/src/bq/infra/migrations/002_owner_auth.sql), and [authentication tests](./bharatquant/tests/test_auth.py): interactive `bq auth setup`, Argon2id password hash, hashed session tokens, 12-hour sessions, persistent lockout after five failures for 15 minutes, host-only HttpOnly/SameSite=Strict cookie, origin and logout CSRF checks. Setup refuses to overwrite a password or accept input when terminal echo cannot be disabled. Existing databases must run `bq db migrate` first.
- [Security audit log](./bharatquant/backend/src/bq/infra/audit.py) and [audit migration](./bharatquant/backend/src/bq/infra/migrations/003_audit_events.sql): redacted owner setup, successful sign-in, lockout and sign-out events; database triggers reject update, delete and replacement, and `bq doctor` verifies the hash chain. It detects partial or accidental changes, not a fully rewritten database without a separately retained trusted checkpoint.
- [Calendar contract](./bharatquant/backend/src/bq/data/calendar.py) and [calendar migration](./bharatquant/backend/src/bq/infra/migrations/004_exchange_calendar.sql): append-only, owner-reviewed date records with source provenance and `known_at`/reviewed/recorded-time as-of selection. No exchange records are seeded; missing coverage raises an error. Additions are recorded in the security audit chain.
- [Calendar coverage endpoint](./bharatquant/backend/src/bq/api.py) and [panel](./bharatquant/web/src/CalendarCoverage.tsx): owner-only, non-cacheable, read-only coverage for 1–31 dates, using one database snapshot and returning the selected retained session ID and times or `UNCOVERED`. The panel requires explicit exchange and date inputs, uses the device clock for `as_of`, and clearly labels uncovered dates unknown and owner review unverified. It cannot edit sessions or establish market/trading readiness. No exchange data is seeded.
- [Calendar record command](./bharatquant/backend/src/bq/cli.py): `bq calendar record-session` requires explicit owner review confirmation, a credential/query/fragment-free HTTPS source, session state and actual timezone-aware boundaries where open. It captures knowledge and review at the local current time, appends the reviewed record and audit event, and uses OS permissions rather than web authentication. Owner review remains an assertion; consult the deferred external-access checklist before entering real rules. No fact is automatically fetched or verified.
- [Point-in-time data record contract](./bharatquant/backend/src/bq/data/records.py) and [data migration](./bharatquant/backend/src/bq/infra/migrations/005_point_in_time_records.sql): append-only provider-neutral records for instruments, EOD bars, snapshots, filings and market statistics, with provenance, quality labels, canonical payload hashes and as-of selection that excludes later inserts. The migration seeds no facts; no live provider adapter or provider-backed ingestion API is enabled. Recorded times depend on the local system clock.
- [Provider request contract](./bharatquant/backend/src/bq/data/providers.py): an internal exact-key, single-kind `as_of` query with a 256-key bound, explicit capability checks, ordered complete results and rejection of duplicate, mismatched or not-yet-known records. Provider errors propagate and unsupported capabilities do not call the provider. Tests use synthetic in-memory providers only. Source identity, capability and availability times are provider assertions; fetching historical-looking records does not establish that the application possessed them at `as_of`. There is no live adapter, ingestion connection, verified terms, calendar operation, range query, pagination, retry, rate limiting or fallback.
- [Effective-dated symbol timelines](./bharatquant/backend/src/bq/data/instruments.py): internal, exact case-sensitive exchange/symbol mapping to instrument IDs using the existing immutable point-in-time record store. Each version has at most 256 non-overlapping, start-inclusive/end-exclusive date intervals; `as_of` filters both knowledge and local record times, and resolution returns the retained record ID and source/quality metadata. Missing dates and malformed newer timelines fail closed. No mapping is seeded; all newly recorded mappings are `UNASSESSED` source assertions, not verified or usable for trading. CSV imports do not yet normalize via these mappings; source authority, cross-source conflicts and independent instrument validation remain open.
- [Symbol-source comparison](./bharatquant/backend/src/bq/data/instruments.py) and `bq data inspect-symbol` compare 1–32 caller-selected source IDs using each source's latest retained version available at `as_of`, read in one SQLite snapshot. Output reports record IDs, per-source instrument IDs and quality, and independently flags uncovered selections and conflicting instruments. The CLI uses OS permissions, not web authentication. A concurrent-write test confirms snapshot consistency. It does not enumerate unselected sources, assess authority, adjudicate conflicts or make an agreement verified; source IDs and owner-supplied records remain assertions. Existing single-source lookup behavior remains unchanged and is not connected to ingestion or trading.
- [Data-to-context adapter](./bharatquant/backend/src/bq/data/context.py): resolves an exact retained record ID only if both its knowledge and record times are available at `as_of`, then builds a deterministic context item with stable source/version IDs, canonical data and the recorded quality label. It omits the source URL from rendered content and accepts URL-safe record IDs including `_` and `-` prefixes. Source metadata, quality, clock and classification are not independently verified; only payload hashes are checked, not metadata integrity. Other caller-created context items remain possible, so a future gateway must enforce trusted source selection. Imported data remains unassessed.
- [Snapshot freshness diagnostic](./bharatquant/backend/src/bq/data/freshness.py) and `bq data inspect-snapshot` compare a caller-selected retained snapshot's event time with `as_of`, after checking knowledge and record times. Callers explicitly provide delayed and critical thresholds and choose `WARN`, `BLOCK` or `ALLOW` for delays; `ALLOW` still labels the snapshot delayed. The CLI bounds thresholds to one year and returns metadata/age without the payload, using local OS permissions rather than the web password. Threshold equality is not exceeded, but critical staleness, non-acceptable quality or missing event time blocks under all modes. Future event/record times, events after publication/receipt and records before knowledge time fail. Quality labels, timestamps, source identity and local clocks remain assertions; no quote provider, policy configuration, readiness check or order flow uses this diagnostic.
- [Snapshot freshness API](./bharatquant/backend/src/bq/api.py) and [panel](./bharatquant/web/src/SnapshotFreshness.tsx) expose the same diagnostic for an explicitly entered retained snapshot ID and caller-selected thresholds. The authenticated read-only endpoint is non-cacheable, bounds inputs and returns source, quality, timestamps, age and policy but not the payload. The panel uses the device clock and makes its non-readiness limitations visible. There is no snapshot provider or record-discovery UI; source, quality and timestamps remain assertions. No execution or order gate uses this diagnostic.
- [Record-backed context replay](./bharatquant/backend/src/bq/data/context.py) reconstructs included prompt entries from retained record IDs at the manifest's `as_of` and checks their content, identity, record/knowledge times, prompt/manifest order and hashes against the stored prompt. It fails on unsupported source types, missing or later versions, duplicates and mismatches. Dropped items are not reconstructed; selection ranking, system/schema, classifications, source authority and local clocks remain assertions. No gateway calls it and it does not protect against a complete database rewrite or establish privacy eligibility.
- [Offline EOD CSV importer](./bharatquant/backend/src/bq/data/csv_eod.py), [CLI](./bharatquant/backend/src/bq/cli.py), and [import migration](./bharatquant/backend/src/bq/infra/migrations/008_data_imports.sql): bounded, atomic imports with explicit source/review provenance, adjustment and currency, decimal-string prices, duplicate-row rejection, idempotency and audit events. The CLI requires explicit terms-review confirmation and runs with the local OS user's permissions, not the web password. Inputs are marked `UNASSESSED`; owner attestation is not a legal review or source-quality verification. There is no CSV upload API, raw-file retention, instrument-master mapping, automatic calendar-aware completeness/outlier verification or trading use.
- [Offline EOD quality diagnostic](./bharatquant/backend/src/bq/data/eod_quality.py) and [CLI](./bharatquant/backend/src/bq/cli.py): caller supplies up to 366 ordered expected dates, a source/instrument/adjustment, an explicit basis-point threshold and either exact retained record IDs or a single import ID. `bq data inspect-eod` checks the import manifest, selects only that import's matching records and prints JSON; it uses local OS permissions, not web authentication. The diagnostic verifies `as_of` availability and series consistency, checks OHLC/volume and units, reports missing dates and flags exact close-to-close changes strictly above the threshold, without comparing across a gap. It neither mutates quality labels nor persists incidents; selected records can be older revisions. Expected dates, threshold and source authority are caller assertions, not a calendar, corporate-action or legal review. No public API, readiness gate or trading integration exists.
- The same command optionally derives expected bars from 1–31 calendar dates using `--calendar-exchange`, `--calendar-start`, `--calendar-end` and explicit `--assume-open-sessions-expected`. It requires coverage for every date and refuses open sessions not yet closed at `as_of` or ranges with no open sessions; output includes session IDs/states for all dates and bars in the selected import on closed dates. Closed-day bars are indicators, not verified errors. This assumes every open session needs a bar for the instrument and does not verify its exchange, listing, suspension or special-session eligibility. Calendar and data reads use separate snapshots. Owner-recorded facts, source authority, local clocks and instrument eligibility are not independently verified; this remains a diagnostic rather than a readiness or trading gate.
- [Authenticated data-status endpoint](./bharatquant/backend/src/bq/api.py) and [panel](./bharatquant/web/src/DataStatus.tsx): `/api/v1/data/status` returns non-cacheable historical import and record counts and up to 20 recent import identifiers, source IDs, UTC timestamps and counts, not imported rows, unique instrument counts or trading readiness. Both counts and history come from one database snapshot. It reports no live provider and labels imported data unassessed.
- [Durable job-run core](./bharatquant/backend/src/bq/infra/jobs.py) and [job migration](./bharatquant/backend/src/bq/infra/migrations/006_job_runs.sql): unique job occurrences, bounded attempts, leased claims with per-claim fencing tokens, expired-lease recovery, stale-worker rejection and append-only run events. Generated URL-safe IDs and lease tokens beginning with `_` or `-` are accepted, including existing stored IDs. Job handlers still need idempotent side effects keyed by occurrence. Schedule triggering, workers, DAG dependency execution and APIs are not implemented.
- [Daily schedule definitions](./bharatquant/backend/src/bq/schedules.py), [snapshot storage](./bharatquant/backend/src/bq/infra/schedules.py), and [schedule migration](./bharatquant/backend/src/bq/infra/migrations/007_schedule_versions.sql): immutable, audited configuration snapshots with optimistic version checks, dependency and implemented-job validation, timezone/window checks and read-only daily previews. A pure wake-policy decision handles on-time, skipped, expired and outside-window occurrences and rejects invalid/ambiguous occurrences. No jobs are registered or executed; session-event triggers, automatic wake processing, deadline validation, dependency execution and schedule editing remain to be implemented.
- [Interval schedules](./bharatquant/backend/src/bq/schedules.py) extend the same immutable snapshots, wake decisions, bounded discovery and status previews. The configured local time is the first occurrence; subsequent occurrences are spaced by 1–1,440 whole minutes, through the inclusive operating-window end, then restart the next local day. Old daily snapshot JSON remains unchanged and readable. DST ambiguity or nonexistent local occurrences fail closed; exceeding discovery limits rolls back the batch. The status panel identifies the interval, start and end. These remain configuration and preliminary wake-policy eligibility only: no dispatch, automatic wake processing, market-day filtering, dependency execution or catch-up safety is implemented.
- Daily and interval schedules can restrict occurrences to an ordered, nonempty set of local weekdays, numbered Monday `0` through Sunday `6`; existing snapshots without the field still mean every day. Previews, bounded discovery and direct wake decisions apply that selection, and the panel shows it. Weekdays are not a trading-day calendar: holidays and special sessions are not filtered.
- [Schedule effective boundaries](./bharatquant/backend/src/bq/infra/migrations/012_schedule_effective_boundaries.sql) and [wake decision ledger](./bharatquant/backend/src/bq/infra/migrations/013_schedule_wake_decisions.sql): new snapshots have audited, append-only, strictly advancing effective times no earlier than their record times. Historical snapshots without a boundary remain readable but cannot be treated as effective; a later unbounded snapshot also makes historical selection fail closed. An explicitly requested occurrence is recorded once with the schedule version effective at its scheduled time; retries return the original decision and recorded observation time. The internal batch operation also discovers daily occurrences for a caller-supplied range, atomically records decisions and fails closed for legacy gaps, ambiguous local times or exceeded limits. It is not automatically invoked. Observation uses the local clock after the database write lock, before enumeration, not a trusted commit clock. `RUN` is preliminary wake-policy eligibility, not permission to execute: dependencies, job-specific catch-up safety and durable dispatch are not implemented. No jobs are enqueued or executed.
- [Wake-to-job dispatch](./bharatquant/backend/src/bq/infra/dispatch.py): `dispatch_one` converts a `WakeDecision.RUN` record into a `JobRun` occurrence using the effective schedule at the occurrence time. `dispatch_due` batches multiple decisions with per-record error isolation. 19 tests in `tests/test_dispatch.py` cover idempotency, error paths, batch continuation and multi-schedule dispatch.
- [Authenticated schedule-status endpoint](./bharatquant/backend/src/bq/api.py) and [panel](./bharatquant/web/src/ScheduleStatus.tsx): owner-only, non-cacheable `/api/v1/schedules/status` presents the latest and effective versions, the first 20 effective schedules and 20 recent wake decisions plus counts, all read from one database snapshot. Pending activation or missing historical boundaries are reported as unknown. For enabled daily or interval schedules, a read-only preview shows the next five occurrences after its observation time, assuming no configuration changes; it is withheld when a future version is pending or the next local time is ambiguous/nonexistent. It ignores calendars, dependencies and wake policies, and is not a guarantee of execution. The panel explicitly identifies `RUN` as wake-policy eligibility, not dispatch, and cannot edit or execute schedules.
- [Context builder](./bharatquant/backend/src/bq/context.py), [manifest store](./bharatquant/backend/src/bq/infra/contexts.py), and [migration](./bharatquant/backend/src/bq/infra/migrations/009_context_manifests.sql): bounded, deterministic section ordering, point-in-time filtering against caller-provided knowledge/record times, duplicate/conflict detection, required-item and section protection, full-prompt budgeting via an injected counter, explicit drop reasons, exact prompt bytes and hashes. The system prompt, output schema and every item require classifications; the rendered-input classification excludes dropped items. The manifest is stored locally in plaintext; hash validation is not protection against a complete database rewrite. Source identities, classifications, timestamps and counter accuracy are not independently verified; dropped content is not retained and source-level reconstruction is not guaranteed. There is no model integration, trusted input classification or prompt-injection isolation. Do not use sensitive content before storage/privacy review.
- [Model eligibility](./bharatquant/backend/src/bq/model_routing.py): rejects unreviewed terms/capabilities and data-handling permissions, private input on any free tier, and local reasoning/multimodal work. A local `SMALL` selection needs a matching task-version and model-fingerprint evaluation. Candidate and task metadata, classifications, review flags and evaluations are caller assertions, not a trusted registry or verified provider integration; no real routes are enabled.
- [Offline context routing](./bharatquant/backend/src/bq/infra/context_routing.py): validates persisted manifest/task/model identity, rendered-item provenance and the rendered classifications before invoking eligibility checks. It fails closed for legacy manifests missing classification metadata and requires rebuilding for another model. It is not a gateway or authoritative privacy boundary; dropped private metadata remains in the manifest and must not be sent wholesale to a provider.
- [Task definitions](./bharatquant/backend/src/bq/task_registry.py), [registry](./bharatquant/backend/src/bq/infra/task_registry.py) and [migration](./bharatquant/backend/src/bq/infra/migrations/011_task_definitions.sql): append-only, exact-version task policies, prompt/schema text and classifications, prompt/spec version identifiers, section limits and required sections, with idempotent writes and audit events. Registered-context routing checks system, schema, versions, classifications and actual rendered section counts against the selected definition. The registry starts empty, is writable by local code rather than an authenticated web session, and stores prompt/schema plaintext. It does not validate JSON Schema semantics, establish source selection or tokenizer accuracy, establish source or provider trust, specify timeouts/fallbacks, or protect against full database rewrites. Low-level routing can bypass the registry; do not dispatch until a gateway enforces trusted registry policy and privacy gates.
- [Offline model-output validation](./bharatquant/backend/src/bq/model_output.py) checks existing task schemas at use time with JSON Schema Draft 2020-12 and an explicitly non-retrieving reference registry. It rejects invalid schemas, unresolved references, invalid JSON, duplicate keys, non-finite numbers, invalid UTF-8 scalar values and outputs over 1 MiB. Numeric comparisons use bounded exact decimals: literals are at most 512 characters and decimal adjusted exponents are limited to -308 through 308; fractional values return `Decimal` and require explicit serialization. No model content appears in the translated error messages. Definitions remain storable even if their schema is semantically invalid, preserving compatibility. Only trusted schemas should be used: regex/runtime complexity is not bounded. `format` is not asserted, and any `$schema` field must name the supported dialect even within literal data. Financial values still require domain-specific unit, precision and range validation. This is not a gateway, citation/fact/privacy check, repair loop or safety approval; it is not called by a model client.
- [Offline registered-output verification](./bharatquant/backend/src/bq/infra/context_routing.py) composes exact registered-task/model eligibility checks, record-backed prompt replay and output schema validation. An optional immutable task `citation_path` specifies an object-key path to a nonempty array of exact source/version identity pairs; the verifier extracts and checks each pair against the replayed prompt, rejecting malformed, duplicate and unknown citations. Existing definitions without the field keep their original canonical representation and receive no citation check. It operates only on caller-supplied output and eligibility assertions, without dispatch. It does not check other paths, prove every claim is cited or entailed, prove which model produced a response, establish trusted source selection or privacy, inspect dropped content, or replace an enforcing gateway.
- [Record citation identity checks](./bharatquant/backend/src/bq/data/context.py) replay the record-backed context and require each supplied source/version pair to have been included in that prompt. Empty or oversized lists and unknown or mismatched versions fail. Inclusion is not claim entailment, source authority, completeness or evidence of privacy. The registered-output helper invokes it only when the task version specifies a citation path; no gateway invokes it.
- [Attempt ledger](./bharatquant/backend/src/bq/infra/model_attempts.py), [usage CLI](./bharatquant/backend/src/bq/cli.py), and [migration](./bharatquant/backend/src/bq/infra/migrations/010_model_attempts.sql): append-only intent and single terminal outcome with durable incomplete-attempt inspection, conflict prevention, consistent read snapshots and explicit reported/estimated/unknown/no-call usage including partial counts. Task/version/model JSON summaries separate reported from estimated totals and count missing input/output values without treating them as zero. The CLI uses local OS permissions, not the web password. An attempt references a stored prompt manifest and requires the same task ID, task version and model ID. No gateway, actual provider, cache, automatic recovery or external usage reconciliation uses it; no-call and usage values remain caller claims. SQLite/local-clock trust and plaintext-manifest limitations still apply.
- [Authenticated usage endpoint](./bharatquant/backend/src/bq/api.py) and [panel](./bharatquant/web/src/UsageStatus.tsx): non-cacheable owner-only summaries for the first 20 task/version/model groups, with total group count and separate reported, estimated and unknown input/output usage. Token totals are serialized as decimal strings and displayed with `BigInt` to preserve precision. No model provider is connected; values are ledger assertions, not provider-verified billing.
- [SPA security headers](./bharatquant/backend/src/bq/api.py): per-response nonce and `strict-dynamic` script policy, no inline-script allowance, explicit same-origin resource policies, and non-cacheable HTML so a cached nonce cannot mismatch its CSP header.
- [Architecture decisions](./bharatquant/docs/architecture-decisions.md): paper-only boundary, local modular monolith, storage authority, market-data gates, task-class routing, durable schedules, local security and third-party reuse constraints.
- [React/TypeScript/Vite shell](./bharatquant/web/): strict type-checking, responsive accessible status view, explicit offline state and retry, same-origin production health fetch, and local system fonts (no external font request). The build is included in the Python wheel.
- [Frontend calendar](./bharatquant/web/tests/calendar.test.mjs), [freshness](./bharatquant/web/tests/freshness.test.mjs) and [schedule](./bharatquant/web/tests/schedule.test.mjs) contract tests: Node's built-in runner checks response identity, state and boundary cases. CI runs `npm test`; TypeScript stripping is experimental on the supported Node 22 line.
- [UTC clock, exchange-time conversion and validated paise/quantity values](./bharatquant/backend/src/bq/core.py): both amounts are nonnegative integers, reject booleans and coercion, and allow zero. Order entry must independently enforce a positive quantity. No ledger or order execution exists yet.
- [Fixed-fractional long sizing](./bharatquant/backend/src/bq/risk/sizing.py) computes whole shares from caller-supplied equity, cash, entry and stop prices in integer paise and a caller-supplied risk fraction in basis points. It floors the risk budget, limits quantity by both nominal stop-distance risk and gross entry cash, and rejects invalid stops and fractions. It sets no risk defaults, uses no verified quotes, accounts for no charges, slippage, gaps, taxes or short margin, and is not connected to accounts, order entry or a risk gate. A stop does not guarantee a maximum realized loss; never treat this calculation alone as permission to trade.
- [Offline binary scoring](./bharatquant/backend/src/bq/reports/scoring.py) computes mean Brier score, log loss and a 95% Wilson interval for the observed positive-outcome rate from up to 100,000 caller-supplied resolved binary outcomes. Empty input yields unavailable metrics; impossible certainty is explicitly counted as unbounded log loss rather than clipped. A separate binary accuracy calculation uses an explicit caller-supplied positive threshold and a Wilson interval. The intervals describe sampled rates, not individual predictions. Calibration bins use explicit caller-supplied edges, with lower-inclusive/upper-exclusive bounds except the final upper bound, and retain empty bins without fabricating averages. Baseline comparisons require equal lengths and matching observed outcomes, but cannot independently verify prediction identities or baseline provenance. The caller must enforce prediction freeze, timing, outcome provenance and eligibility; coverage, small-sample labels, persistence and reports do not exist. No market-stance resolver is implied.
- The same scoring module has a dated-input helper that rejects duplicate or future-created IDs, excludes predictions created at or after the evaluation window starts, and counts timely resolutions only when their asserted resolution time is at or before `as_of` and not before the window ends. UTC-instant comparison handles DST folds. Unresolved timely predictions and late predictions are counted separately. These input timestamps and outcomes remain caller assertions; this is not an immutable prediction registry, record-backed source check, full planned coverage or a historical model-cutoff check.
- [Foundation tests](./bharatquant/tests/test_foundation.py) and a [lock-to-notice consistency test](./bharatquant/tests/test_dependency_notices.py). `bq serve` rejects a frontend directory missing its `index.html`, rather than starting with a broken shell.
- [Project setup documentation](./bharatquant/README.md), [dependency/license inventory](./bharatquant/THIRD_PARTY_NOTICES.md), npm lockfile, ignored generated static build output, and Hatch configuration to include that output in wheels.
- Pinned Hatchling at 1.33.0 in the build-system requirement and development lock; CI builds with `--no-build-isolation` to use the locked backend.
- npm is used instead of the planned pnpm because pnpm was absent and Corepack failed signature validation; the signature failure was not bypassed.
- [GitHub Actions workflow](./bharatquant/.github/workflows/ci.yml) configures Ubuntu and `macos-15` Apple Silicon checks, frontend install/build/audit, Python lint/type/tests, lock verification, idempotent CLI initialization, and wheel asset, freshness, model validator, scoring, sizing, dependency metadata and all thirteen migration checks. `npm audit` now runs in the correct `web` directory. Actions are pinned to release commit SHAs; the four selected action repositories were checked at those commits and report MIT licenses. CI pins uv to 0.12.13. **Correction (current snapshot): this workflow file is not present in the repository — see the corrections block below.**
- [External-source gates](./bharatquant/docs/external-source-gates.md) record checks and blockers: NSE pages timed out, Google AI documentation was denied by network policy, and DeepSeek documentation returned HTTP 403. No restricted access was bypassed; related provider and market behavior remains disabled.

Verification on this Mac used Python 3.12.7 because Python 3.13 is not installed.
The combined schedule, scheduler-runner and dispatch slices pass **74 tests**,
and the changed Python files pass Ruff lint and format checks. The full Python
3.13 suite and Node 22 frontend checks remain to be run on the target
environment. The previous checkpoint passed **223 pytest tests** and **9
frontend tests**; it did not include the lifecycle loop changes above.

After adding `ALLOW`, Node 23.1.0 passed **9 frontend tests**, typecheck and an isolated Vite production build. Earlier, the in-project frontend build and offline npm audit passed. An isolated synthetic HTTP server and browser exercised the snapshot panel's successful result and invalid-threshold message before `ALLOW` was added; it did not test real authentication or a live provider. The temporary server was stopped. Pylance syntax checks passed for the changed Python files before `ALLOW` was added. The full backend suite, in-project frontend build and packaged wheel have not been re-run after the final `ALLOW` change.

Earlier checks verified a wheel containing registered-output verification, freshness, time-aware/calibration/baseline scoring, sizing, model-output validation, the SPA index and thirteen migrations. Registered-output tests compose routing, exact task/schema identity, record replay and schema failure. Freshness tests cover the CLI's exact output, threshold boundaries, delayed modes, stale/quality blocks and inconsistent or unavailable timestamps. Scoring tests cover known Brier/log-loss/Wilson values, threshold accuracy, empty samples, impossible certainties, calibration boundaries, baseline alignment, late/pending predictions, as-of boundaries, DST folds and invalid/bounded inputs. Sizing tests cover risk and cash limits, flooring, zero, invalid inputs, large values and a range of integer invariants. Output tests cover exact decimal constraints and bounds, decimal context isolation, local and remote references, invalid stored schemas, nested duplicate keys, non-finite numbers, malformed/oversized/deep JSON and non-disclosing errors. Citation tests cover included and dropped records, other versions, forged content and bounded input. Tests also cover independently selected symbol sources, conflicting and uncovered results, corrections, as-of boundaries, malformed records, CLI output and concurrent snapshot consistency; scheduler tests exercise inclusive windows, a 1,000-occurrence limit, DST ambiguity/nonexistence, excluded weekdays, invalid occurrences, snapshot version changes, batch rollback, idempotency, authenticated preview and absence of dispatch. Earlier tests cover point-in-time data and context, calendar-aware EOD, record-backed replay, model routing and usage, leases, CSV imports and migration preservation. Pylance selected the host Python rather than the isolated test environment and reports missing `uvicorn`, `jsonschema` source and `referencing` imports; runtime and strict mypy checks used the isolated environment. Pylance diagnostics for context routing, freshness, scoring, sizing and the citation module were clean. A temporary synthetic HTTP server and browser accessibility snapshot verified the calendar panel's open/closed/uncovered states, invalid-range and invalid-response errors, and narrow viewport without horizontal overflow. An earlier synthetic check covered daily schedule preview; screenshot capture failed, so visual appearance was not inspected. Earlier isolated Edge checks covered private import and usage views, login and logout. Temporary servers were stopped.

The workflow has not run on GitHub or Apple Silicon hardware.

## Checkpoint — dark violet theme + bundled fonts + auto sign-in

- Backend auth tests were extended to cover the auto-sign-in contract
  (`state == "signed_in"`, `set-cookie`, valid session after setup). The
  full Python 3.13 suite passes **301 tests**. Changed Python files pass
  Ruff lint; pre-existing format violations in unrelated files were left
  alone.
- Frontend: 9 contract tests pass under Node 22.1.0. Typecheck and the
  production Vite build succeed. Self-hosted fonts (Inter 4.1 and JetBrains
  Mono 2.304, SIL OFL 1.1) ship under `bharatquant/web/public/fonts/`;
  their licenses are recorded in
  `bharatquant/THIRD_PARTY_NOTICES.md` and ship alongside.
- Manual browser checks (127.0.0.1:8000) confirmed: (a) first visit renders
  the setup form, creates the owner password and lands on the dashboard
  without a separate sign-in; (b) a returning visitor sees the welcome-back
  sign-in form, signs in and reaches the dashboard; (c) the violet accent,
  Inter headings and mono labels render consistently at 1440px and 390px
  widths without horizontal overflow.


The workflow has not run on GitHub because the workspace has no Git repository; the Mac job and Node 22 frontend test execution are configured, not verified. Equivalent wheel-content checks passed locally; `actionlint` was unavailable for a recheck of the updated workflow (the earlier version passed actionlint 1.7.12). Hosted macOS CI may incur account charges. The inventory records package/license metadata and locked registry hashes but is not a legal review; full notices for redistribution and the BharatQuant project license remain undecided. WebSocket protections and a password-recovery workflow do not exist. Every future private route must explicitly enforce authentication, and state-changing routes must enforce origin and CSRF checks. Still pending: Apple Silicon macOS operation, provider or market-rule claims, and private-data and trading behavior. This is not an operational trading app.

## Reproduce the checkpoint

On the target Mac, from `bharatquant/`:

```sh
uv sync --locked --python 3.13
cd web
npm ci
npm test
npm run build
npm audit
cd ..
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q
```

On this Windows/OneDrive development host, clear inherited `VIRTUAL_ENV` and set `UV_LINK_MODE=copy`. The project-local `bharatquant\.venv\Scripts\bq.exe` was locked by another process, so dependency synchronization and checks used the isolated environment `C:\Users\apatha08\.copilot\session-state\6ccb5309-4c8d-4c32-a97f-865ae2c68ffa\files\schema-validation-env` via `UV_PROJECT_ENVIRONMENT`. Do not stop or interfere with the process holding the executable. Resync the project environment when it is safe; it does not yet include the new validator dependencies. Do not touch the unrelated `databricks_test` environment.

## Latest checkpoint — news research agent (first agent)

Added the first agent, a **news research agent** backed by **Pulse by Zerodha**
(`https://pulse.zerodha.com/feed.xml`). Designed for cost, not cleverness:
**zero model calls** — ingest, deduplication and classification are all
deterministic.

- **Source**: `bq/infra/news_feed.py` fetches the RSS feed over HTTPS with a
  2 MB byte cap, a 15 s timeout and no redirects beyond httpx defaults. Verified
  live: one feed is ~107 KB carrying 156 stories, each with a title and a
  one-line summary — so **full articles are never fetched**.
- **Parsing** (`bq/news.py`): stdlib `ElementTree`. Bounded to 500 items, 512
  title chars and 1,024 summary chars. A feed claiming a future `pubDate` is
  clamped to ingest time rather than trusted.
- **Deduplication, no LLM**: every item is keyed by `url_hash` (normalised
  HTTPS URL) and `shingle_hash` (order-insensitive title fingerprint). A second
  ingest of the same feed stored 0 items and reported all 156 as duplicates.
- **Classification, no LLM**: whole-word keyword rules map each item to
  `INDIA_MARKET`, `GLOBAL_MARKET`, `GEOPOLITICAL`, `COMMODITY`, `RATES`,
  `CORPORATE`, `REGULATORY` or `OTHER`. Whole-word matching matters: earlier
  substring matching classified "celebrated" as a rate story.
- **Agent memory**: `agent_memories` keeps **long-term-impact** items only.
  Structural categories (geopolitical, regulatory) qualify outright; rate items
  qualify only on structural words (repo, policy rate, rate cut/hike, inflation)
  so routine RBI notices do not flood memory; other categories need an explicit
  marker. Live run: **15 memories from 156 items**. Memories carry `known_at`
  and `expires_at`, are filtered point-in-time, and track `hit_count` for later
  frecency pruning.
- **Storage** (`022_news_agent.sql`): `news_items` and `agent_memories`, both
  `STRICT`, with immutability triggers. Measured: 156 rows / 31 KB of text and
  15 memories / 4.5 KB in a 598 KB database.
- **Interfaces**: `bq news ingest` and `bq news digest --limit N` on the CLI;
  owner-only, non-cacheable `GET /api/v1/news/digest` in the API.

Verification: **330 backend tests** pass (26 new in `tests/test_news.py`, plus a
runtime-dependency guard), **13 frontend tests**, ruff clean, frontend build
clean. Live ingest and digest were exercised against the real Pulse feed, then
local state was removed.

An authenticated Settings tab consolidates configuration: `GET
/api/v1/settings/overview` reports registered model tasks, effective schedules,
scheduler state and model-usage groups, alongside explicit not-configured states
for providers, API keys and agents. It is deliberately read-only; the copy says
so rather than implying controls that do not exist.

Tests that hardcoded a migration count now derive it from
`packaged_migrations()`, so adding a migration no longer breaks unrelated
suites.

### Packaging fix: undeclared runtime imports

A clean install exposed a real defect. `bq.cli` imports the news agent, which
imports `httpx`, but `httpx` was declared only in the development dependency
group. A production `pip install .` therefore produced a `bq` command that could
not start. `httpx` is now a runtime dependency.

Two further direct imports were reaching the runtime through `fastapi` rather
than being declared: `pydantic` and `starlette`. They are now declared
explicitly, because a package you import should not depend on another package's
transitive closure to stay resolvable.

`tests/test_runtime_dependencies.py` guards this going forward. It walks the
import graph reachable from `bq.cli` and fails if any module imports a top-level
package that is not in `[project].dependencies`. It is import-graph based, so it
runs in milliseconds; ADR-010 records the heavier clean-install check
(`python -m venv`, `pip install .`, `python -c "import bq.cli"`) for release
preparation.

### Environment note: hidden `.pth` files

The recurring `ModuleNotFoundError: No module named 'bq'` is now understood and
worked around at the project boundary. Python's `site.py` skips any `.pth` file
carrying the macOS **hidden file flag** (`st_flags & UF_HIDDEN`), and uv's
editable-install `.pth` inside `.venv` can end up with that flag set. Nothing in
this project set it, a fresh venv in `/tmp` is clean, and `uv sync` does not
re-apply it, so the origin is environmental rather than a project defect.

The `justfile` now exports `PYTHONPATH` to `bharatquant/backend/src`, and pytest
is configured with `pythonpath = ["backend/src"]`. Both were verified with the
flag deliberately forced to `hidden`: `just init`, `just doctor`, `just run` and
the full suite all pass. Commands therefore no longer depend on venv internals.

## Corrections to the historical record above

The block above predates the current tree and several statements are no longer
true. Kept for history; read these instead:

| Earlier claim | Current state |
|---|---|
| "Python 3.13 is not installed"; suite verified on 3.12.7 | 3.13.18 is installed and the suite runs on it |
| "previous checkpoint passed 223 pytest tests" | **616** backend tests pass |
| "9 frontend tests" | **31** frontend tests pass |
| "thirteen migrations" | **25** migrations; the count is now derived in tests rather than hardcoded |
| "CI builds with `--no-build-isolation`"; workflow pinned to SHAs | **No CI workflow file exists** in this repository snapshot; recreate before relying on hosted checks |
| "the workspace has no Git repository" | It is a Git repository on branch `dev` |
| Dependencies: no `httpx` at runtime | `httpx`, `pydantic` and `starlette` are **runtime** dependencies; a production install previously could not start `bq` |
| `bq` commands fail after install | Fixed at the project boundary: `justfile` and pytest export `PYTHONPATH` to the source tree |

## Resume next

1. **Wire the news agent into the analysis pipeline.** `_news_context` is a
   placeholder that reports `degraded — not wired in this slice`, which the new
   agent trace now makes visible on every run. It should read `agent_memories`
   filtered to `as_of` so the debate sees point-in-time news instead of nothing.
   That is the single biggest remaining gap in the "multi-agent" story.
2. **Run the batch concurrently, bounded.** `BatchAnalyst` is deliberately
   sequential (see its docstring): ten symbols against one HTTP client trades a
   rate-limit risk for latency that does not matter for a manual batch. If the UI
   ever needs it, add a bounded `asyncio.TaskGroup` fan-out in `batch.py` with a
   per-host token bucket, not in the pipeline.
3. **Give the paper executor an account snapshot reader.** Right now the caller
   supplies `ExecutionContext` by hand (CLI flags, tests). It should be derived
   from `PaperAccountStore.get_balance`, `paper_positions`, today's realised loss
   and the calendar's session state, so `bq paper execute` needs only the plan
   and an account id. That also removes the chance of an operator mis-typing a
   limit. Keep the limits themselves explicit and owner-reviewed.
4. **Make the news agent a scheduled job.** `bq news ingest` runs standalone; it
   is not a registered job or schedule entry. Add it to the job catalogue and a
   daily schedule so ingest happens in the operating window, then expose
   long-term memories to the morning report.
5. **Review the swing/stop heuristics against real outcomes.** The 1.5×ATR stop
   and the ATR-bounded support logic are plausible but unvalidated. They are
   deterministic and reviewable by design; treat them as a starting point, not a
   proven rule. The freshness thresholds (`SWING_MAX_STALE_WEEKDAYS`,
   `INTRADAY_CRITICAL_AFTER_SECONDS`) are likewise unvalidated starting points.
6. Keep private data protected: every new private route must enforce authentication and every state-changing request must enforce origin and CSRF. Add and test WebSocket origin/auth controls if WebSockets are introduced; there are no WebSockets yet.
7. **Improve news classification only with an explicit budget.** `OTHER` is the
   largest bucket and first-match labelling mislabels headlines that mention two
   topics. Any model pass must be opt-in, capped by tokens, and applied to the
   ambiguous subset — never a per-article call during ingest.
7. **P1 offline complete**: scheduler lifespan, dependency dispatch, paper accounts, exact-paise OMS, reconciliation, TWR/XIRR and universe publishing are implemented and tested. Before P1 acceptance on the target Mac, run the full backend suite and frontend build in Python 3.13/Node 22, verify external market/provider claims and provider adapters remain disabled, and add schedule/operator controls if job handlers are later enabled.
8. Verify market/provider claims through authorized access before implementing features that rely on them. Complete legal review/full license notices if redistribution is planned and decide the project license.
9. Run GitHub CI and test on the target Apple Silicon Mac, including frontend tests on Node 22. The CI workflow referenced in earlier checkpoints is **not present in this repository snapshot**; recreate it before relying on hosted checks. Do not add trading pages or mock balances. Keep P0 in progress and do not claim P1 acceptance until prerequisites and acceptance checks pass. Extend symbol normalization with independently reviewed source authority and conflict handling, then integrate it into ingestion without treating unassessed mappings as verified. Extend calendar-aware diagnostics with verified instrument/session eligibility, source authority and quality incidents before using them as readiness gates. Extend the provider contract for calendar, range and other use cases only with explicit provenance and coverage semantics; implement automatic wake processing and session/dependency semantics around the schedule and job-run stores. Before connecting wake decisions to execution, validate dependency occurrence identities, job-specific catch-up safety and durable dispatch, and prevent hindsight entries. Do not enable real adapters or seed external market facts before authoritative-source and terms gates pass.
10. Continue P2 by making task definitions authoritative at the gateway boundary, including enforceable source-selection and citation requirements, output validation, route/fallback and timeouts; establish trusted model/privacy eligibility evidence and source classification, trusted source-version lookup, validated model-specific tokenization and comprehensive source-level context replay, including dropped content. Citation paths are currently optional, check only the configured array and do not establish claim coverage. Define the output serialization policy and resolve untrusted-schema resource limits before relying on output validation. Build a gateway that owns attempt accounting and cannot bypass registry policy. Keep model clients disabled until privacy, provider terms and source gates are verified. Protect plaintext task and manifest content and add prompt-injection defenses before accepting real data.
11. Continue P5 with owner-reviewed risk limits, accounts, order state transitions, atomic reservations and independent safety gates before connecting the sizing calculation to any order flow. Keep short selling, execution and live trading disabled until their prerequisites are verified.
12. Continue P3 after its P1/P2 prerequisites: frozen point-in-time predictions, evidence-backed outcome revisions, coverage and small-sample reporting, calibration/baselines and immutable reports. The isolated binary-score helper is not a prediction registry or report workflow.
