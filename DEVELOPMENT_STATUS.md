# BharatQuant — compact handoff

Updated 26 November 2026. This file keeps only what is needed for the **next**
task. History lives in git and
[`architecture-decisions.md`](./bharatquant/docs/architecture-decisions.md).

## Current state

- Startup fixed (26 Nov 2026): the local dev DB had been initialised from a
  pre-commit draft of `034_reports.sql` (its `prediction_details.kind` CHECK
  lacked `STANCE`), so `bq init`/`doctor` aborted with "Migration history
  differs". Repaired in place; owner password, Gemini key and settings preserved.
  Backup at `~/Library/Application Support/BharatQuant/bq.db.bak-*`. A fresh DB
  applies all 37 migrations cleanly. `_applied_count` now names the first
  divergent migration.
- Single-owner, paper-only Indian equities system. **No broker order path**; Kite
  is read-only. Paper execution is the only execution path.
- Phases 1–19 are implemented: decision/Jev layer, agents, Strategy desk, paper
  execution/realism, memory, confidence, opportunities/shadows, scheduler,
  reports, backtests, research dossiers and runtime hardening.
- **Verification:** 868 backend tests, 112 frontend tests; ruff/format clean;
  frontend typecheck/build clean; fresh DB applies **37 migrations**.
- **mypy:** 25 pre-existing errors in `cli.py`, `paper_performance.py` and
  `infra/scheduler.py`. Do not “fix” them as part of maintenance.
- **Next migration number:** `038` if a new migration is needed.

## Phase 19 delivered

- `bq/runtime.py`: owner runtime snapshot (DB/WAL/free disk, integrity,
  migration count, job leases/stale jobs, table counts), Prometheus text metrics,
  verified online SQLite backup with SHA-256 manifest, guarded restore with a
  `bq.db.pre-restore` copy, and deterministic `bq bench` smoke timings.
- CLI: `bq db backup`, `bq db restore --confirm`, `bq bench`; just targets
  `bench`, `backup` and `restore`.
- API: `GET /api/v1/system/status`, `GET /api/v1/system/metrics` and the
  owner-authenticated `WS /api/v1/events` runtime snapshot feed.
- Today page: Runtime card for health, storage, job leases and issues.
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
| Runtime | `bq/runtime.py`, `bq/cli.py`, `web/src/RuntimePanel.tsx` |
| Operations | `docs/runtime-operations.md`, `justfile` |
| API/WS | `bq/api.py` system status/metrics/events |
| Research | `bq/research_expansion.py`, `bq/fundamentals.py` |
| Strategies | `bq/strategy_dsl.py`, `bq/backtest.py` |
| Execution | `bq/paper_trading.py`, `bq/execution/` |

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
```
