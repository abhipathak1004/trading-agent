# BharatQuant — compact handoff

Updated 26 November 2026. This file keeps only what is needed for the **next**
task. History lives in git and
[`architecture-decisions.md`](./bharatquant/docs/architecture-decisions.md).

## Current state

- Single-owner, paper-only Indian equities system. **No broker order path**; Kite
  is read-only. Paper execution is the only execution path.
- Phases 1–18 are implemented and acceptance-tested: decision/Jev layer, agents,
  Strategy desk, paper execution/realism, all four memory layers, confidence,
  opportunities/shadows, scheduler, reports, backtests, and point-in-time
  research dossiers with deterministic valuation and claim review.
- **Verification:** 862 backend tests, 109 frontend tests; ruff/format clean;
  frontend typecheck/build clean; fresh DB applies **37 migrations**.
- **mypy:** 25 pre-existing errors in `cli.py`, `paper_performance.py` and
  `infra/scheduler.py`. Do not “fix” them as part of the next task.
- **Next migration number:** `038`.

## Phase 18 delivered

- Migration `037_research_expansion.sql`: append-only `research_notes`,
  `fundamental_snapshots`, `thesis_events`, `research_claims` and
  `research_claim_reviews`.
- `bq/research_expansion.py`: point-in-time notes (NEWS/YouTube/XBRL/FILING/OWNER)
  with publication and known-at bounds; news-item import; thesis evidence and
  invalidation events; owner claim review queue; dossier assembly.
- `bq/fundamentals.py`: deterministic XBRL/owner fundamental snapshots, ratios
  and integer-paise DCF. Model text never supplies these numbers.
- `NewsStore.get` supports converting an ingested item into a research note.
- API: notes, fundamentals, dossier, theses/events and claim review. Owner auth;
  writes require CSRF; reads are `no-store`.
- Research page: dossier lookup, point-in-time note form, XBRL/fundamental
  snapshot form, thesis evidence display and claim accept/reject.
- Tests: note point-in-time/future rejection, ratios/DCF determinism, thesis
  evidence, claim review, dossier/API and UI contracts.
- Fixed App restoration: Reports and Backtests page sections remain wired.

## Task 19 — hardening and runtime (next)

1. WebSocket/event feed for job, paper and research updates.
2. Metrics/health endpoints and a lightweight benchmark suite.
3. Backup/restore and retention checks for SQLite/WAL and report archives.
4. Sleep/wake and launchd guidance without duplicate workers.
5. Chaos/E2E checks for worker leases, restarts and stale schedules.
6. Migration `038_*` if needed; update this handoff and add an ADR.

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
- Schedule times/windows are configuration, never UI hard-coding.

## Relevant module map

| Area | Files |
|---|---|
| Research | `bq/research.py`, `bq/research_expansion.py`, migration `037` |
| Fundamentals | `bq/fundamentals.py` |
| Strategies/backtests | `bq/strategy_dsl.py`, `bq/strategy_store.py`, `bq/backtest.py` |
| Execution | `bq/paper_trading.py`, `bq/execution/` |
| API/UI | `bq/api.py`, `web/src/ResearchDossierPanel.tsx` |
| Tests | `tests/test_research_*.py`, `test_backtest*.py` |

## Commands

```sh
cd bharatquant
PYTHONPATH=backend/src uv run --locked pytest -q
PYTHONPATH=backend/src uv run --locked ruff check backend/src/bq tests
PYTHONPATH=backend/src uv run --locked ruff format --check backend/src/bq tests
PYTHONPATH=backend/src uv run --locked mypy backend/src/bq

cd web
npm test && npm run typecheck && npm run build

# fresh DB
tmp=$(mktemp -d)
BQ_HOME="$tmp" PYTHONPATH=backend/src uv run --locked bq init
BQ_HOME="$tmp" PYTHONPATH=backend/src uv run --locked bq doctor
```
