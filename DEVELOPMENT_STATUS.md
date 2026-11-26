# BharatQuant — current handoff

Updated 26 November 2026. This is the **compact** working context for the next
task. History, rationale and superseded notes live in git and
[`architecture-decisions.md`](./bharatquant/docs/architecture-decisions.md); do not
carry the old status narrative forward.

## Where the project is

BharatQuant is a single-owner, paper-only Indian equities research system. Phases
1–3F are implemented: config-driven model tiers, optional TypeSafe Jev decision
layer, Agent Lab prompt/playbook editing, Strategy desk with a deterministic
Super Agent, specialist agents, final gate, episodic/semantic/procedural memory,
Brier calibration with an outcome resolver, and the evidence verifier with an
optional Jev Noul pass.

- **No broker order path exists.** Kite is a read-only data vendor (ADR-001,
  ADR-013). Paper execution is the only execution path.
- **Verification now:** 784 backend tests, 65 frontend tests; ruff + format
  clean; frontend typecheck + build clean; fresh DB applies **30 migrations**.
- **mypy:** 25 pre-existing errors in `cli.py`, `paper_performance.py` and
  `infra/scheduler.py`. Do not “fix” them as part of feature work.
- **Next migration number:** `031`.

## Code map (read these first)

| Area | Files |
|---|---|
| Entry points | `bq/api.py`, `bq/cli.py`, `bq/__main__.py` |
| Orchestration | `bq/analysis/pipeline.py`, `bq/analysis/batch.py`, `bq/super_agent.py` |
| Decision layer | `bq/model_router.py`, `bq/decision/` |
| Specialists / gates | `bq/analysis/{news_context,regime,fundamental,final_gate,evidence}.py` |
| Memory | `bq/memory/{episodic,semantic,procedural,resolver}.py` |
| Risk / execution | `bq/risk/{policy,sizing,decisions}.py`, `bq/execution/paper_executor.py`, `bq/paper_accounts.py` |
| Config | `config/agent_settings.yaml`, `bq/settings.py`, `bq/agent_prompts.py` |
| UI | `bharatquant/web/src/App.tsx`, the `*Panel.tsx` files and `*Contract.ts` files |

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
- **Security:** owner auth, origin + CSRF on writes, `no-store` on private reads.
- **Paper only.** Any Kite order method, sandbox order or `LIVE_AUTO` mode is out
  of scope until the ADRs are explicitly superseded. External adapters fail closed.
- **No new runtime dependency** without a clear need.

## Immediate next task — Phase 4A: make paper execution reachable

The gate (`evaluate_pretrade`), paper OMS and CLI path already exist. The missing
piece is the **owner-facing seam**: derive the account snapshot, expose
gate/execute over the API, and wire Strategy accepted plans into it. Do not add a
broker adapter in this slice.

### Deliverables

1. **Account snapshot reader** — add `bq/execution/context.py` (or a method on the
   paper-account store) that builds `ExecutionContext` from the existing paper
   tables: balance/cash, open positions, symbol exposure, realised daily/weekly/
   monthly loss, orders this minute, calendar session state, kill switch and
   instrument validity. Keep limits explicit/owner-reviewed; do not invent them.
   Every amount stays integer paise.
2. **Execution seam** — `bq/execution/base.py` with `ExecutionAdapter`,
   `ExecutionMode` (`PAPER`, `KITE_SANDBOX`, `LIVE_APPROVAL`), `ExecutionResult`
   and a typed disabled error. `PaperExecutionAdapter` wraps the existing
   `PaperExecutor` unchanged and relays the plan quantity. The sandbox/live
   adapters raise a typed “not enabled” error; no broker call.
3. **API** — owner-only, origin + CSRF, `no-store`:
   - `POST /api/v1/execution/preview` — plan + account/context + explicit risk
     limits → gate decision, freshness and reasons; never places an order.
   - `POST /api/v1/execution/paper` — places only on `ALLOW`; idempotency key
     required/replayed; returns paper order + decision. Reject `SELL`/`HOLD`.
   - Reuse the existing paper-account read endpoints for balances/positions; do
     not create a second account model.
4. **Strategy wiring** — accepted plans from `POST /api/v1/strategy/run` can be
   sent to preview/execute without re-typing numbers. The Strategy accepted-plan
   card gets a **Preview gate** / **Place paper order** action; the server
   re-validates the plan JSON (`trade_plan_from_json`).
5. **Trade tab** — replace the inert Trade nav item with a working panel: account,
   plan, limits, gate reasons, freshness, order result. Show `BLOCK` reasons
   verbatim; make the paper-only boundary explicit. No mock balances, no broker
   fields.
6. **Config** — if persisted risk limits are needed, add migration
   `031_risk_limits.sql` plus a Settings card. Otherwise pass explicit limits per
   request and say so in the UI. Do not silently default risk.

### Acceptance

- Gate blocks: stale/unusable data, closed session, missing stop, invalid
  instrument, kill switch, cash/notional/position/rate limits, daily/weekly/
  monthly loss limits, and R:R below the agreed floor.
- Only `ALLOW` reaches `PaperExecutor`; quantity is unchanged; a duplicate
  idempotency key returns the original result.
- `KITE_SANDBOX` / `LIVE_APPROVAL` fail closed with a typed error.
- Strategy accepted plan → preview → paper order works end-to-end from the UI.
- Add backend + frontend tests; keep 784+/65+ green; ruff/format/typecheck/build
  clean; fresh `bq init` applies 31 migrations (or 30 if no new table).

### Deferred, not this slice

- Working memory as a dedicated `run_state` table (the run ledger already keeps
  `analysis_runs.detail_json`).
- Registering the outcome resolver on a schedule (`POST /api/v1/memory/resolve`
  and the Today button are manual today).
- Semantic-doc ingestion API/UI (the store/search path exists).
- Jev Noul threshold (0.60), playbook content and freshness limits are
  unvalidated starting points; calibrate from outcomes later.

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
