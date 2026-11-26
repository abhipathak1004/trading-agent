# BharatQuant — compact handoff

Updated 26 November 2026. This file keeps only what is needed for the **next**
task. History lives in git and
[`architecture-decisions.md`](./bharatquant/docs/architecture-decisions.md).

## Current state

- **Responsive UI (26 Nov 2026):** the shell now works on phones and tablets.
  Below 1024px the sidebar is an off-canvas drawer — `web/src/Sidebar.tsx`
  (wrapping `App.tsx`) owns the state, a backdrop, `inert` when closed, body
  scroll lock, ESC/backdrop/item-select close, focus onto the active item and
  back to the toggle, and a 44px target floor. The old ≤800px horizontal
  nav strip and its `.nav-label` suppression are gone, so group headings
  survive at every width. All 22 `.judgment-table` instances (11 panels) are
  wrapped in `web/src/TableScroll.tsx`, a labelled, focusable
  `role="region"`; under 768px the table keeps a 620px minimum and the first
  column is sticky, so data scrolls inside the card and the page never
  scrolls sideways. `web/index.html` carries `viewport-fit=cover`, `100dvh`
  pairs back every `100vh`, safe-area insets pad the sidebar/footer, and
  fields are 16px under 640px so iOS Safari no longer zooms on focus.
  Verified in Chrome via CDP with a signed-in session at 320/390/430/768/1024/
  1280/1440 across all 10 mounted pages: **zero horizontal overflow**, drawer
  open/close/focus/labels correct at every width ≤1024, desktop unchanged.
  `web/tests/responsive.test.mjs` guards the invariants.
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
- Fix: the panel re-arms its poll effect until a terminal state (bounded
  `MAX_POLLS`, keeps last known status through fetch errors, both buttons usable
  when stalled). The manager now gates on cloudflared's `Registered tunnel
  connection` line and probes the **loopback origin**, not the public name;
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
  typecheck, 116 tests and the production build pass.
- Single-owner, paper-only Indian equities system. **No broker order path**; Kite
  is read-only. Paper execution is the only execution path.
- Phases 1–19 are implemented: decision/Jev layer, agents, Strategy desk, paper
  execution/realism, memory, confidence, opportunities/shadows, scheduler,
  reports, backtests, research dossiers and runtime hardening.
- **Verification:** 872 backend tests, 119 frontend tests; ruff/format clean;
  frontend typecheck/build clean; fresh DB applies **37 migrations**.
- **mypy:** 90 pre-existing errors across `cli.py`, `paper_performance.py`,
  `infra/scheduler.py` and the tunnel test fakes. Do not “fix” them as part of
  maintenance; the tunnel change adds none.
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
