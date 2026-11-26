# BharatQuant — current handoff

Updated 26 November 2026. This is the **compact** working context for the next
task. History, rationale and superseded notes live in git and
[`architecture-decisions.md`](./bharatquant/docs/architecture-decisions.md); do not
carry the old status narrative forward.

## Where the project is

BharatQuant is a single-owner, paper-only Indian equities research system. Phases
1–11 are implemented and acceptance-tested: config-driven model tiers, optional
TypeSafe Jev decision layer, Agent Lab prompt/playbook editing, Strategy desk with
a deterministic Super Agent, specialist agents, final gate,
episodic/semantic/procedural/working memory, Brier calibration with a scheduled
outcome resolver, the evidence verifier with an optional Jev Noul pass, a deterministic
paper execution/fill/cancel/exit lifecycle, owner risk-limit presets, owner-driven
semantic ingestion, a bounded job worker, and average-cost realised P&L on exits.

- **No broker order path exists.** Kite is a read-only data vendor (ADR-001,
  ADR-013). Paper execution is the only execution path.
- **Paper orders are owner-filled.** Placement creates PENDING; the Trade tab
  fills at the order limit, cancels pending orders, exits held positions with a
  SELL LIMIT order, and reports realised P&L against average cost.
- **Scheduled resolver is opt-in.** No schedule exists on a fresh install. The
  owner enables the daily `memory.resolve` schedule from Memory calibration.
- **Verification now:** 835 backend tests, 90 frontend tests; ruff + format
  clean; frontend typecheck + build clean; fresh DB applies **31 migrations**.
- **mypy:** 25 pre-existing errors in `cli.py`, `paper_performance.py` and
  `infra/scheduler.py`. Do not “fix” them as part of feature work.
- **Next migration number:** `032`.

## Code map (read these first)

| Area | Files |
|---|---|
| Entry points | `bq/api.py`, `bq/cli.py`, `bq/__main__.py` |
| Orchestration | `bq/analysis/pipeline.py`, `bq/analysis/batch.py`, `bq/super_agent.py` |
| Decision layer | `bq/model_router.py`, `bq/decision/` |
| Specialists / gates | `bq/analysis/{news_context,regime,fundamental,final_gate,evidence}.py` |
| Memory | `bq/memory/{episodic,semantic,procedural,working,resolver}.py` |
| Jobs | `bq/infra/{jobs,job_worker,job_handlers,scheduler,dispatch}.py` |
| Tunnel | `bq/infra/tunnel.py` |
| Risk / execution | `bq/risk/{policy,sizing,decisions}.py`, `bq/execution/{base,context,paper_executor}.py` |
| Paper stores | `bq/paper_accounts.py`, `bq/paper_trading.py` |
| Config | `config/agent_settings.yaml`, `bq/settings.py`, `bq/agent_prompts.py` |
| UI | `web/src/App.tsx`, `TradePanel.tsx`, `RiskLimitsPanel.tsx`, `SemanticMemoryPanel.tsx`, `MemoryCalibrationPanel.tsx`, the other `*Panel.tsx` files and `*Contract.ts` files |
| Acceptance | `tests/test_phase5_acceptance.py` |

## Invariants (do not break)

- **Jev is optional.** Ladder: `Jev → Flash → Reasoning → heuristic → RULES_ONLY`.
  Missing key, 4xx/5xx, timeout, invalid answer or model mismatch falls through.
  No keys still completes deterministically.
- **No arithmetic in a model.** Indicators, R:R, sizing, P&L, exposure and
  thresholds are computed in Python and passed as structured data.
- **The Coordinator is code, not an LLM.** Point-in-time `as_of`, bounded inputs,
  deterministic fan-out, no invented symbols.
- **Super Agent asymmetry:** it may downgrade a BUY to HOLD/AVOID, never create a
  BUY or change a plan number. Plan quantities are never resized by the
  coordinator.
- **Prompts/playbooks are text configuration.** Question names, options, score
  levels, thresholds, sizing and risk limits are code-owned.
- **Memory excludes** live quotes, OHLCV candles, open positions and margins.
  Reads are point-in-time; episodes are immutable and outcomes are append-only.
  Semantic documents are immutable and owner-supplied; no web fetch or model call
  creates them. Working memory stores decision state, never raw quotes, OHLCV,
  positions or margins.
- **Security:** owner auth, origin + CSRF on writes, `no-store` on private reads.
  Loopback stays the default; the tunnel host/origin allowlists are empty unless
  a quick tunnel is live, and any rejection fails closed (ADR-037).
- **Paper only.** Any Kite order method, sandbox order or `LIVE_AUTO` mode is out
  of scope until the ADRs are explicitly superseded. Sandbox/live adapters fail
  closed.
- **Risk limits are explicit.** The Trade tab requires all six gate limits. A
  saved preset only pre-fills them; each preview/place request still carries the
  values. There are no invented defaults and no auto-placement.
- **Fills are owner-triggered.** There is no matching engine, no market order and
  no short position. A fill is atomic across order, position and ledger.
- **Exits are owner-triggered SELL LIMIT orders.** Quantity is capped at the held
  position; the exit does not use the BUY pre-trade gate.
- **P&L is derived, not double-booked.** SELL fills report average-cost realised
  P&L in the response; the ledger remains the cash authority.
- **Jobs use one lease/fencing store.** The worker dispatches only registered
  handlers; no second queue, no scheduler bypass.
- **No new runtime dependency** without a clear need.

## Latest slices

**Cloudflare quick tunnel (ADR-037).** `bq/infra/tunnel.py` owns a runtime-only
`cloudflared` quick tunnel: `stop/start/status` under
`/api/v1/settings/tunnel`, spawned with no shell, URL-health-checked before it is
shown, stopped on app shutdown. `TunnelHostMiddleware` and `verify_origin` admit
the tunnel host/origin **only while starting/live**, so the loopback default and
fail-closed behavior are unchanged; the session cookie turns `Secure` for tunnel
logins. Binary resolves `BQ_CLOUDFLARED` → `PATH` → `BQ_HOME/bin`, with an
owner-triggered verified download from Cloudflare's GitHub release. Settings has
a **Cloudflare tunnel** panel with Start / Stop / Copy link. No migration, no
persisted setting, no new dependency, owner-password access only.

**Phase 4A — execution seam.** `bq/execution/context.py` builds the gate inputs
from one snapshot (cash, positions, average-cost realised losses, order rate,
calendar session, instrument validity; paused accounts fail closed).
`bq/execution/base.py` enables only PAPER; sandbox/live raise `ExecutionDisabled`.
`PaperExecutor.preview()` gates without placing; `execute()` preserves quantity,
enforces the 1.5 R:R floor and replays a duplicate client order id.

**Phase 4B — fill and reconcile.** `POST /api/v1/paper/orders/{id}/fill` is atomic
and idempotent; `GET /api/v1/paper/accounts/{id}/reconciliation` checks order fill
totals, fill totals, ledger totals and open positions.

**Phase 5 — end-to-end acceptance.** `tests/test_phase5_acceptance.py` drives the
real HTTP surface with a deterministic market vendor: three-symbol batch →
specialists → evidence → final gate → persisted runs → Strategy → accepted plan →
execution preview → paper order → fill → reconciliation → calibration endpoint.
The fixture sets `abstain_score_band=(2, 2)` because the synthetic monotonic trend
reaches conviction 3 and the production default `(2, 3)` abstains. This is a
test-owner policy, not a production default change.

Live smoke on 26 November 2026:
- `NSE:RELIANCE` produced a plan with no gate veto.
- `NSE:TCS` and `NSE:HDFCBANK` produced plans but the heuristic final gate vetoed
  them with `conviction 2 in abstain band`.
- This is the conservative default behaving as designed, not a wiring failure.

**Phase 6 — cancel and exit.** `POST /api/v1/paper/orders/{id}/cancel` cancels a
pending/partial order. `POST /api/v1/paper/accounts/{aid}/positions/{instrument}/exit`
creates a SELL LIMIT order for part or all of a held position; the existing fill
flow credits exact paise. Over-holds and shorts are rejected.

**Phase 7 — risk-limit presets.** `bq/settings.py` stores an optional validated
`risk_limits` JSON setting in `app_settings`. `GET/PUT/DELETE
/api/v1/settings/risk-limits` are owner-only and CSRF/origin protected. The
Settings card saves/resets the six limits; the Trade tab pre-fills from the
preset, still sends explicit limits on every request, and permits per-run
overrides. A fresh install has no preset.

**Phase 8 — semantic ingestion.** `POST /api/v1/memory/semantic` stores
owner-supplied research text through `SemanticMemoryStore.ingest`. It requires
HTTPS without credentials, rejects future `known_at`, bounds every field, and
returns the existing document for duplicate content. Agent Lab's Semantic memory
panel now has an ingest form beside search; no web fetch and no model call.

**Phase 9 — worker and scheduled resolver.** `bq/infra/job_worker.py` claims
`JobRun` rows with the existing lease/fencing contract, dispatches by `job_id`,
and succeeds/fails atomically. `bq/infra/job_handlers.py` registers
`memory.resolve` with the 5-day horizon and bounded episode count. The API
lifespan starts the worker loop alongside scheduler discovery. A daily
`memory-resolve-daily` schedule is created only when the owner enables it from
Memory calibration; a fresh install has none. `/api/v1/schedules/status` now
includes recent job runs, and the schedule panel displays them.

**Phase 10 — realised P&L.** `PaperTradingStore.fill` computes average-cost
realised P&L for SELL fills from prior fills, returns it on `PaperFill`, and
recomputes it on idempotent replay. BUY fills return null. Partial exits keep the
remaining cost basis; a full exit clears it. The fill API and Trade notice show
the signed result. No second ledger and no migration.

**Phase 11 — working memory.** Migration `031` adds append-only
`working_memory`, keyed by `(run_id, symbol)` so a Strategy run can hold one row
per analysed symbol. `bq/memory/working.py` builds a typed `WorkingState` with
routing, specialists, evidence, debate, final gate, plan and technical summary;
`state_json` is capped at 64 KiB and hashed. Single, batch and Strategy paths
persist it after the run ledger; a memory failure is isolated. Owner-only
`GET /api/v1/analysis/runs/{id}/working-memory` feeds the expandable Technical and
Strategy views. No raw quotes/OHLCV/positions/margins and no second run ledger.

Verification: **835 backend tests**, **90 frontend tests**, ruff/format clean,
frontend typecheck + build clean. Fresh DB applies **31 migrations**. mypy remains
at 25 pre-existing errors in untouched files.

## Immediate next task — Phase 12: confidence engine and setup base rates

The confidence primitives exist but nothing produces an evidence card. Build the
deterministic front half before any report or opportunity work.

1. **Evidence producers** — map structure, volume, momentum, relative strength and
   market context into `bq.confidence.Evidence` from the existing technical report
   and specialist briefs. One family counts once; structure and volume are
   mandatory.
2. **Base rates** — add a bounded setup-study engine over daily bars and a
   `setup_base_rates` table (migration `032`) keyed by setup × horizon × regime.
   No DSL and no LLM. `min_base_rate_samples` remains a hard gate.
3. **Assessment** — wrap `assess_confidence` with policy config, provisional
   haircut, EV/R, vetoes and VIX step-down; expose a preview API/UI.
4. **Review** — optional `confidence.review` through the decision ladder; it may
   only lower or veto, never raise.
5. **Tests** — mandatory structure+volume, 4-of-7 confluence, insufficient-history
   veto, EV floor, provisional/calibrated labels, PIT bars.
6. **Boundary** — all arithmetic in Python; no order placement and no model-sourced
   probability without a code-owned calibration path.

### Deferred, not this slice

- Jev Noul threshold (0.60), playbook content, freshness limits and the final-gate
  abstain band remain unvalidated starting points; calibrate from outcomes later.

## Commands

```sh
# backend
cd bharatquant
PYTHONPATH=backend/src uv run --locked pytest -q
PYTHONPATH=backend/src uv run --locked ruff check backend/src/bq tests
PYTHONPATH=backend/src uv run --locked ruff format --check backend/src/bq tests
PYTHONPATH=backend/src uv run --locked mypy backend/src/bq

# frontend
cd bharatquant/web
npm test
npm run typecheck
npm run build        # regenerates backend/src/bq/static

# fresh database
tmp=$(mktemp -d)
BQ_HOME="$tmp" PYTHONPATH=backend/src uv run --locked bq init
BQ_HOME="$tmp" PYTHONPATH=backend/src uv run --locked bq doctor
```

Use `just` for the packaged pipeline. Do not commit generated `static/assets` by
hand; build the frontend instead.
