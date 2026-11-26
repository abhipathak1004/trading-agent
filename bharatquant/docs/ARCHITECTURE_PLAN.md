# Architecture plan: 3-tier model routing and the multi-agent system

**Version:** 1.4 · **Date:** 2026-11-26 · **Status:** approved for Phase 2
**Scope:** audit of the existing BharatQuant analysis stack and the target
architecture for a config-driven three-tier model router, Jev-preferred decision
layer, specialist agents, four-layer memory and a deterministic execution seam.

This document is the in-repo companion to the implementation plan. It records
what exists, what changes, and which invariants must not move. It does not claim
any external provider capability is verified; see
[external-source-gates.md](./external-source-gates.md).

## Changelog

| Version | Date | Change |
|---|---|---|
| 1.4 | 2026-11-26 | Added the Strategy desk: a deterministic Super Agent coordinator runs the per-symbol agents, enforces cash/position limits, and asks the reasoning tier only to rank and comment. It can downgrade, never create, a BUY. |
| 1.3 | 2026-11-26 | Added Agent Lab: a decision-preview surface and append-only, template-validated prompt versions for the active text agents plus the decision fallback. Question sets, options, thresholds and risk rules stay code-owned. |
| 1.2 | 2026-11-26 | Studied the production Jev reference `buberlo/jev-trader` (MIT) and adopted its separation: dense deterministic `DecisionState` → one atomic Choice/Noul/Score battery → code-owned `DecisionPolicy` → hard risk vetoes → calibration triples. Added a deterministic `HeuristicJudge` fallback, pinned-model/latency/request-id capture, deadline-aware no-retry, and ECE/reliability/Platt calibration. |
| 1.1 | 2026-11-26 | Jev made optional: decision questions run on Jev when configured and reachable, otherwise on the configured Flash/Reasoning tier as validated JSON, otherwise deterministically (`RULES_ONLY`). Added the availability ladder and Brier bucketing by decision source. |
| 1.0 | 2026-11-26 | Initial audit and target architecture. |

## 1. What exists today

| Area | Module | Notes |
|---|---|---|
| DAG runner | `bq/agents.py` (`AgentNode`, `AgentDAG`) | Topological layers, deterministic sequential execution, optional nodes, cycle checks. |
| Analysis pipeline | `bq/analysis/pipeline.py` (`AnalysisPipeline`, `PipelineContext`, `AnalysisResult`, `AgentTrace`) | Nine nodes: `gather_data`, `news_context`, `technical`, `bull`, `bear`, `judge`, `risk_team`, `size`, `decision`. `news_context` is a placeholder; every stage is mandatory. |
| Technical analysis | `bq/analysis/technical.py`, `bq/compute/indicators.py` | Deterministic pandas/numpy indicators; four bounded sub-scores; model never computes a number. |
| Debate | `bq/analysis/debate.py` | Bull, bear, judge. One call per role. Judge confidence is clamped by `confidence_ceiling`. |
| Risk team | `bq/analysis/risk_team.py` | Advisory only; cannot veto. |
| Plan contract | `bq/analysis/decision.py` (`TradePlan`) | Revalidates on read; rejects any quantity the sizer did not produce; LIMIT-only, long-only. |
| Model calls | `bq/llm_client.py` → `bq/llm_service.py` (`LLMService.complete`) | One global provider/model from `app_settings`; Gemini or OpenAI-compatible wire format; injectable transport. |
| Routing gate | `bq/model_routing.py` (`select_model`) | Eligibility/terms/privacy check; first eligible candidate wins. |
| Settings | `bq/settings.py`, migration `023` | `app_settings` key/value + `provider_api_keys`; masked secrets; owner-only API and Settings UI. |
| Run audit | `bq/analysis/run_settings.py`, migration `025` | Immutable per-run record with plan hash and `detail_json` (64 KiB). |
| News | `bq/news.py`, `bq/news_agent.py`, migration `022` | Deterministic RSS ingest, dedupe, keyword classification, `agent_memories` with `known_at/expires_at`. |
| Market data | `bq/data/vendors/*`, `bq/data/symbols.py` | Per-domain chains; Yahoo default; Kite read-only when enabled (ADR-013). |
| Strategy coordinator | `bq/super_agent.py`, `POST /api/v1/strategy/run` | Per-symbol DAG → deterministic gate → reasoning synthesis (rules fallback). Never places an order. |
| Prompt config | `bq/agent_prompts.py`, migration `027` | Append-only versions; code defaults; template placeholder validation. |
| Execution | `bq/execution/paper_executor.py` | Paper orders only, behind `evaluate_pretrade`. |
| Scoring | `bq/reports/scoring.py` | Brier/log-loss/Wilson helpers; offline only, unwired. |

## 2. Target graph

```
Stage 0  screener (decision tier)         ── skip low-quality setups cheaply
Stage 1  news │ technical │ fundamental │ regime  (flash, deterministic cores)
Stage 2  evidence verifier (decision tier) → bull/bear debate (reasoning)
Stage 3  super agent / CIO (reasoning) → final gate (decision tier)
Stage 4  deterministic risk gate → paper execution (no LLM)
```

The coordinator remains a deterministic Python DAG (`AgentDAG`), never a model.
Stage 1 nodes are optional at the DAG level so one specialist failure degrades
rather than kills a run.

## 3. Model tiers

| Tier | Purpose | Default | Fallback |
|---|---|---|---|
| `decision` | Choice / Score / Noul answers | TypeSafe Jev `jev-1.14.0` | Flash → Reasoning → deterministic heuristic → `RULES_ONLY` |
| `reasoning` | Thesis synthesis, debate, reflection | `gemini-2.5-pro` | Flash |
| `flash` | Extraction, summarisation, formatting | `gemini-2.5-flash` | Reasoning |

`TierName` is deliberately separate from `ModelTier` (`LOCAL`/`FREE`/`PAID`).
The latter is part of the eligibility gate and the canonical task-definition
JSON; adding members there would break stored definitions.

### Decision availability ladder

1. Jev native when the route allows it and a `TYPESAFE_JEV` key exists.
2. Otherwise the same questions are sent to Flash as JSON matching a generated
   schema.
3. Otherwise Reasoning, same schema.
4. Otherwise the node records `rules_only` and the deterministic pipeline
   continues.

Every step is visible in the run ledger (`decision_source`, `fallback_used`,
`fallback_from`) and in the trace. Fallbacks are configuration, never a silent
substitution.

### Reference: how Jev is used in production

`buberlo/jev-trader` (MIT, surveyed 2026-11-26) is the reference for this
separation:

> Code calculates the state. Jev interprets the state. Code applies policy.
> Execution places the order. The hard risk layer always wins.

Its loop is the shape we adopt: a deterministic feature engine builds a dense
`<400`-token numeric state, one Jev call answers six atomic judgments (Choice,
Noul, Score), a code policy maps those judgments to an action, a hard risk
engine vetoes, and every `(state, decision, outcome)` triple is logged for
Brier/log-loss/ECE/reliability/Platt calibration.

Our modules mirror that split:

| Concern | Module |
|---|---|
| Deterministic state | `bq/decision/state.py` (`DecisionState`) |
| Atomic question battery | `bq/decision/battery.py` |
| Jev transport, pinned model, latency | `bq/decision/client.py` |
| Deterministic fallback | `bq/decision/fallback.py` (`HeuristicJudge`) |
| Code-owned thresholds | `bq/decision/policy.py` (`DecisionPolicy`) |
| Calibration triples and metrics | `bq/decision/calibration.py` |
| Hard risk (existing, adapted later) | `bq/risk/policy.py` + `evaluate_pretrade` |

No source is copied: structure only, original implementation (ADR-008/011).
The async SDK, Avellaneda–Stoikov pricing, crypto venue loop and
`pydantic-settings` are deliberately not reused.

## 4. Contracts

Existing, unchanged in meaning: `TechnicalReport`, `SubScore`, `Argument`,
`Verdict`, `RiskAssessment`, `TradePlan`, `AgentTrace`.

Planned additions (Phase 3+):

| Contract | Fields |
|---|---|
| `SpecialistBrief` | `agent`, `symbol`, `as_of`, `facts`, `claims`, `degraded` |
| `Claim` | `statement`, `evidence_ids`, `verified` |
| `TradeThesis` | `proposed_action`, `entry_zone`, `invalidation_level`, `target_level`, `horizon`, `key_risks`, `thesis_summary` |
| `DecisionAnswer` | typed Choice/Score/Noul answer + probability + source |
| `TradeOutcome` | executed or shadow, entry/exit, R-multiple, regime, predicted `p_exec` |

Phase 2 adds `RouteUse` (`agent_id`, `task_id`, `tier`, `provider`, `model`,
`decision_source`, `fallback_used`, `fallback_from`) and emits it in
`AnalysisResult.to_dict()["model_routing"]`.

## 5. Memory (Phase 3)

| Layer | Storage | Rule |
|---|---|---|
| Working | run ledger (`analysis_runs.detail_json`) | per-run snapshot, immutable |
| Episodic | `episodic_memory` + FTS5 | executed and shadow trades, Brier by source |
| Semantic | `semantic_docs` + FTS5 | chunked filings/news, `published_at <= as_of` |
| Procedural | `skills/*.md`, `config/regime_weights.json` | versioned, code-loaded |

FTS5 is probed at startup; a deterministic term index is the fallback. No quotes,
candles, positions or margins are ever stored in agent memory.

## 6. Invariants

1. Paper-only; Kite remains read-only (ADR-001, ADR-013).
2. Deterministic code owns every number, including Brier and sizing.
3. The sizer owns quantity; a model may not choose it.
4. `as_of` point-in-time filtering on every memory/news read.
5. A model may only lower confidence, never raise the ceiling.
6. One owner, loopback, masked secrets.
7. No new runtime dependency without updating the notices and the guard test.
8. No model tier is mandatory; every LLM/Jev path has a deterministic floor.

## 7. Not changing in Phase 2

`kite_vendor.py`, `paper_executor.py`, `risk/policy.py`, `analysis/debate.py`,
`analysis/technical.py`, `analysis/decision.py`, the memory tables, and the
existing `/api/v1/llm/complete` path. `settings.save_config` keeps its signature.

## 8. Phases and acceptance

| Phase | Deliverable | Acceptance |
|---|---|---|
| 1 | This document, ADR-016/017, source-gate note, status handoff | Docs reviewed; no source change |
| 2 | Tier settings, `ModelRouter`, `bq/decision` battery, structured fallback, heuristic floor, Settings UI | Model swap via settings only; Jev-down, model-mismatch and no-model runs complete; suite green |
| 3 | Specialists, four-layer memory, shadow trades, Brier, Super Agent + gate | Screener skips without reasoning spend; Brier has no look-ahead |
| 4 | Risk-gate extensions + `ExecutionAdapter` seam, Trade tab | Paper path unchanged; sandbox/live fail closed |
| 5 | Mocked end-to-end runs on RELIANCE/TCS/HDFCBANK | All acceptance checks in the implementation plan pass |
