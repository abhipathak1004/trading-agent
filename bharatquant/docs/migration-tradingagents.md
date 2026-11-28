# Migrating TradingAgents patterns into BharatQuant

**Status:** Phase 1 audit (approval gate). No code has been copied or changed by
this document.

**Purpose:** map every component of
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
that is relevant to an Indian equities technical-analysis agent onto this
repository, and state exactly what is adapted, what is skipped, and what already
exists here in a stricter form.

## Reference points

| Item | Value |
| --- | --- |
| Upstream project | `TauricResearch/TradingAgents` |
| Reference tag | **`v0.7.1`** — commit `c4544913a14ed3df5f1ed7b9e8483270ce20682a` |
| Why this tag | Python/LangGraph architecture. `v1.0.x` is a TypeScript research terminal; this repository is Python, so the `v1.0.x` terminal and the Tauric API are not reusable. |
| Upstream license | Apache-2.0 |
| Reuse level | **R3 — prompts and design reference** (plan §4.1) |
| Licence mode | `PERSONAL` (plan §4.1): used by one owner on one machine, not distributed |

### What R3 means here, concretely

- We study the **structure**: the actor roles, the adversarial debate, the
  bounded tool loop, the look-ahead discipline, the typed vendor-failure model.
- We write **original code** implementing that structure, to this project's
  conventions (the same policy recorded in ADR-011 and ADR-012).
- Adapted prompts carry an `SOURCE` attribution marker and an entry in
  `THIRD_PARTY_NOTICES.md`.
- We do **not** copy source files. There is no `bq/_vendor/tradingagents/`.

The prompt that commissioned this work said "copy & extend". The owner chose
"adapt, reimplement" instead, because plan §4.3.1 already scopes TradingAgents
as R3 and ADR-008 requires original code. This document is the record of that
decision (see also ADR-013).

---

## Component map

### Adapted (structure studied, original implementation)

| Upstream path (v0.7.1) | What we take | Local home | Notes |
| --- | --- | --- | --- |
| `dataflows/router.py`, `dataflows/errors.py` | Per-domain vendor chain with a priority order and typed failures (`VendorUnavailableError` vs `NoMarketDataError`) so a caller can distinguish "try the next vendor" from "this symbol has no data" | `bq/data/vendors/router.py`, `bq/data/vendors/errors.py` | Ours is explicit-registration driven, matching `bq/tools/registry.py`. No config-file indirection. |
| `dataflows/symbols.py` | The idea that a ticker has several vendor-specific spellings resolved from one canonical identity | `bq/data/symbols.py` | Adds NSE/BSE exchange, and marks indices as non-tradeable. |
| `agents/analysts/market_analyst.py` | Technical analyst role and the "compute indicators, then interpret" split | `bq/analysis/technical.py`, `bq/compute/indicators.py` | Indicators are deterministic pandas; the model receives numbers only. |
| `agents/analysts/turn.py`, `agents/tools.py` | Bounded tool loop with a max-rounds cap | **Already have**: `bq/agent_runtime.py` (`MAX_TURNS`) and `bq/tools/registry.py` | No new code. The existing loop is stricter (typed errors, policy per profile). |
| `agents/researchers/bull_researcher.py`, `bear_researcher.py` | Two opposing advocates over the same evidence | `bq/analysis/debate.py` | Round count configurable; 1 for intraday, 2 for swing. |
| `agents/managers/research_manager.py`, `portfolio_manager.py` | A judge that reconciles the two sides, and a separate decision stage | `bq/analysis/debate.py` (`ResearchJudge`), `bq/analysis/decision.py` | Upstream emits a free-text recommendation; ours is a validated structured contract. |
| `agents/risk_mgmt/{aggressive,neutral,conservative}_debator.py` | A risk team that enumerates what invalidates the setup | `bq/analysis/risk_team.py` | **Advisory only.** Upstream's risk debate is the last word; here the deterministic `bq/risk/policy.py` gate remains the only veto (plan §3, principle 11). |
| `graph/analyst_execution.py` | Analysts that are independent run concurrently and synchronise before the decision | `bq/analysis/pipeline.py` over the existing `bq/agents.py::AgentDAG` | `AgentDAG` already runs per-layer nodes in parallel and enforces that a `risk` node is never optional. |
| `agents/schemas.py` | Typed contracts at the model boundary | `bq/analysis/decision.py` (Pydantic v2) | Pydantic is already a declared runtime dependency. |
| `agents/post_screen.py`, `agents/rating.py` | A post-decision sanity screen before anything is reported | `bq/analysis/decision.py` validation | We fold the screens into the contract's own validation rather than a separate pass. |
| `default_config.py` | Everything that matters is configuration, not a constant | `bq/settings.py` (`app_settings`) | Reuses this project's bounded, audited config store. |
| `dataflows/date_window.py`, `tests/test_*_lookahead.py`, `test_news_lookahead.py`, `test_social_lookahead.py` | An explicit analysis date with no future leakage, and tests that prove it | `bq/analysis/pipeline.py` (`as_of`), `tests/test_analysis_pipeline.py` | We mirror the *test intent*: a decision scored on a historical date must contain nothing dated after it. |
| `memory/log.py`, `memory/reflection.py` | A durable record of what ran, with what inputs | `bq/analysis/run_settings.py`, `bq/infra/model_attempts.py` | The attempt ledger already exists; the run store adds per-run provenance. |
| `dataflows/vendors/yahoo/*` | Yahoo as the delayed fallback with a stale-bar guard | **Already have**, stricter: `bq/data/yahoo.py` | Bounded body, typed errors, drops incomplete rows, writes point-in-time records. See "Why not yfinance" below. |

### Skipped (with reasons)

| Upstream path | Why not |
| --- | --- |
| `graph/` (`trading_graph.py`, `setup.py`, `checkpointer.py`, `propagation.py`, `conditional_logic.py`) | LangGraph runtime. `bq/agents.py` is a ~200-line dependency-free DAG that already enforces the invariants we need. Adding LangGraph to a modular monolith with no other graph users is pure cost. |
| `llm_clients/` (≈15 files: `openai_client`, `anthropic_client`, `bedrock_client`, `azure_client`, `google_client`, `chatgpt_auth`, `model_catalog`, …) | `bq/llm_client.py`, `bq/llm_service.py` and `bq/llm_retry.py` already cover the two providers this project uses, with an injectable transport for tests and no vendor SDKs. Porting seven provider SDKs would break `ADR-010`'s dependency discipline. |
| `dataflows/vendors/{sec_edgar,reddit,stocktwits,fred,polymarket,alpha_vantage}` | US-only filings, US social feeds, US macro series and prediction markets. No bearing on NSE/BSE equities. India equivalents (nselib, filings) are already scoped in plan §11.1 and are out of scope here. |
| `cli/` (display, prompts, selections, updates, prefs) | Terminal chrome and branding. This project has `bq` and a React SPA; no CLI chrome is wanted. |
| `report_html.py`, `assets/` | Presentation layer for upstream's report. Not reused. |
| `backtest.py` | Backtesting is a separate work package here (P6, plan §13) with its own two-engine design and parity tests. Not in scope. |
| `memory/settlement.py` | Upstream's position settlement; this project has `bq/paper_trading.py` with exact-paise accounting and reconciliation tests. |
| `portfolio.py` | Portfolio accounting here is `bq/paper_accounts.py` + `bq/paper_trading.py`. |
| `Dockerfile`, `docker-compose.yml`, `.env.example` | Deployment artefacts for a different runtime. |

### Already present in a stronger form (no work)

| Requirement from the commissioning prompt | Where it already exists |
| --- | --- |
| Deterministic indicators, never LLM arithmetic | Being added in `bq/compute/indicators.py`; the *policy* is plan §3 principle 11 and already enforced in `bq/risk/sizing.py`. |
| `max_tool_rounds` cap | `bq/agent_runtime.py` `MAX_TURNS` / `run_agent_loop(max_turns=...)`. |
| Parallel analyst fan-out that synchronises | `bq/agents.py::AgentDAG` levels. |
| Structured trade plan rather than free text | `bq/analysis/decision.py` (new) plus the existing `TradeIntent` shape in plan §9.4. |
| Pre-trade risk gate as deterministic code | `bq/risk/policy.py::evaluate_pretrade` — already takes `data_usable`, `session_open`, `stop_set`, `instrument_valid`. |
| Position sizing from risk, not from a model | `bq/risk/sizing.py::size_long_fixed_fractional`. |
| Point-in-time correctness | `bq/data/records.py`, `bq/data/context.py`, `bq/data/freshness.py`, migration `005`. |
| Run metadata logging for audit | `bq/infra/model_attempts.py` + new `bq/analysis/run_settings.py`. |
| Typed vendor errors, no silent fallback | `bq/data/vendors/errors.py` (new) + the existing precedent in `bq/data/yahoo.py::YahooError`. |

---

## Explicit non-goals carried from this repository's decisions

These are not oversights; they are accepted architecture that the commissioning
prompt would otherwise have overridden.

- **No live order placement.** ADR-001 and plan §2.2 forbid it. Kite Connect is
  reused **only** as a market-data vendor (`kite.quote`, `kite.ohlc`,
  `kite.historical_data`, `kite.instruments`). No `place_order`, no autoslice, no
  sandbox order path, no `LIVE_AUTO`. See ADR-013.
- **No `yfinance` dependency.** `bq/data/yahoo.py` already fetches and validates
  Yahoo bars with bounded input, typed errors and point-in-time persistence. A
  second Yahoo client would be two code paths to the same endpoint with
  different quality guarantees.
- **No LangGraph, no vendor SDKs, no vendored upstream source.**

---

## Why the vendor router is new code rather than an extension of `bq/data/providers.py`

`bq/data/providers.py` defines a deliberately narrow contract: an exact-key,
single-kind, completeness-required `as_of` query for **retained point-in-time
records**. It rejects a partial result. That is correct for replaying a stored
context, and wrong for "give me the last 200 daily bars", where a partial answer
is a normal outcome and the caller wants the freshest vendor that can answer.

The vendor router therefore has a different shape (capability domains, ordered
fallback, `is_realtime` provenance) and lives beside the existing contract
rather than inside it. `providers.py` is untouched.

---

## Attribution to record

| Component | Upstream | License | How recorded |
| --- | --- | --- | --- |
| Analyst / debate / judge / risk-team prompt structure | TradingAgents v0.7.1 (`c4544913`) | Apache-2.0 | `SOURCE` constant in `bq/analysis/prompts.py`; row in `THIRD_PARTY_NOTICES.md` |
| Vendor routing and typed-error model | TradingAgents v0.7.1 | Apache-2.0 | Design note in ADR-013 |
| Look-ahead test intent | TradingAgents v0.7.1 | Apache-2.0 | Design note in ADR-013 |

No upstream file is copied into this repository, so the "Vendored source"
table in `THIRD_PARTY_NOTICES.md` gains no row.
