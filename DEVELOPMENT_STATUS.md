# BharatQuant — current handoff

Updated 26 November 2026. This is the **compact** working context for the next
task. History, rationale and superseded notes live in git and
[`architecture-decisions.md`](./bharatquant/docs/architecture-decisions.md); do not
carry the old status narrative forward.

## Where the project is

BharatQuant is a single-owner, paper-only Indian equities research system. Phases
1–6 are implemented and acceptance-tested: config-driven model tiers, optional
TypeSafe Jev decision layer, Agent Lab prompt/playbook editing, Strategy desk with
a deterministic Super Agent, specialist agents, final gate,
episodic/semantic/procedural memory, Brier calibration with an outcome resolver,
the evidence verifier with an optional Jev Noul pass, and a deterministic paper
execution, fill, cancel and exit lifecycle.

- **No broker order path exists.** Kite is a read-only data vendor (ADR-001,
  ADR-013). Paper execution is the only execution path.
- **Paper orders are owner-filled.** Placement creates PENDING; the Trade tab
  fills at the order limit, cancels pending orders, and exits held positions with
  a SELL LIMIT order.
- **Verification now:** 805 backend tests, 74 frontend tests; ruff + format
  clean; frontend typecheck + build clean; fresh DB applies **30 migrations**.
- **mypy:** 25 pre-existing errors in `cli.py`, `paper_performance.py` and
  `infra/scheduler.py`. Do not “fix” them as part of feature work.
- **Next migration number:** `031` (not needed for Phase 6).

## Code map (read these first)

| Area | Files |
|---|---|
| Entry points | `bq/api.py`, `bq/cli.py`, `bq/__main__.py` |
| Orchestration | `bq/analysis/pipeline.py`, `bq/analysis/batch.py`, `bq/super_agent.py` |
| Decision layer | `bq/model_router.py`, `bq/decision/` |
| Specialists / gates | `bq/analysis/{news_context,regime,fundamental,final_gate,evidence}.py` |
| Memory | `bq/memory/{episodic,semantic,procedural,resolver}.py` |
| Risk / execution | `bq/risk/{policy,sizing,decisions}.py`, `bq/execution/{base,context,paper_executor}.py` |
| Paper stores | `bq/paper_accounts.py`, `bq/paper_trading.py` |
| Config | `config/agent_settings.yaml`, `bq/settings.py`, `bq/agent_prompts.py` |
| UI | `web/src/App.tsx`, `TradePanel.tsx`, the other `*Panel.tsx` files and `*Contract.ts` files |
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
- **Security:** owner auth, origin + CSRF on writes, `no-store` on private reads.
- **Paper only.** Any Kite order method, sandbox order or `LIVE_AUTO` mode is out
  of scope until the ADRs are explicitly superseded. Sandbox/live adapters fail
  closed.
- **Risk limits are explicit.** The Trade tab requires all six gate limits; there
  are no invented defaults. Session overrides are labelled `owner_override`.
- **Fills are owner-triggered.** There is no matching engine, no market order and
  no short position. A fill is atomic across order, position and ledger.
- **Exits are owner-triggered SELL LIMIT orders.** Quantity is capped at the held
  position; the exit does not use the BUY pre-trade gate.
- **No new runtime dependency** without a clear need.

## Latest slices

**Phase 4A — execution seam.** `bq/execution/context.py` builds the gate inputs
from one snapshot (cash, positions, average-cost realised losses, order rate,
calendar session, instrument validity; paused accounts fail closed).
`bq/execution/base.py` enables only PAPER; sandbox/live raise `ExecutionDisabled`.
`PaperExecutor.preview()` gates without placing; `execute()` preserves quantity,
enforces the 1.5 R:R floor and replays a duplicate client order id.

**Phase 4B — fill and reconcile.** `POST /api/v1/paper/orders/{id}/fill` is atomic
and idempotent; `GET /api/v1/paper/accounts/{id}/reconciliation` checks order fill
totals, fill totals, ledger totals and open positions. The Trade tab fills the
remaining quantity and refreshes cash/positions.

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
flow credits exact paise. The Trade tab has per-position exit quantity/price,
Cancel, Fill, and reconciliation controls. Over-holds and shorts are rejected.

Verification: **805 backend tests**, **74 frontend tests**, ruff/format clean,
frontend typecheck + build clean. mypy remains at 25 pre-existing errors in
untouched files.

## Immediate next task — Phase 7: owner risk-limit presets

The Trade tab currently asks for all six gate limits on every run. Persist an
owner-reviewed preset without making it an invisible default.

1. **Store** — add a validated `risk_limits` JSON setting in `app_settings` (no
   migration needed). Keep it absent on a fresh install; do not invent values.
2. **API** — owner-only `GET/PUT /api/v1/settings/risk-limits`, origin + CSRF,
   no-store. Return `configured: false` until the owner saves a preset.
3. **Settings UI** — a card with the six integer paise/count fields and an
   explicit save/reset. Show that these are gate presets, not orders.
4. **Trade UI** — load the preset when present, prefill the six fields, and still
   send explicit limits on every preview/place request. The owner can override a
   field for one run. Do not auto-place anything.
5. **Tests** — absent preset, save/validate/reset, round-trip through Settings,
   Trade prefill/override, and security checks.
6. **Boundary** — risk policy remains code-owned; the preset only supplies request
   values. No migration expected; if one is added, use `031`.

### Deferred, not this slice

- Registering the outcome resolver on a schedule (`POST /api/v1/memory/resolve`
  and the Today button are manual today).
- Working memory as a dedicated `run_state` table (the run ledger keeps
  `analysis_runs.detail_json`).
- Semantic-doc ingestion API/UI (the store/search path exists).
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
