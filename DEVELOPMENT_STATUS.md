# BharatQuant — compact handoff

Updated 26 November 2026. This file keeps only what is needed for the **next**
task. History lives in git and
[`architecture-decisions.md`](./bharatquant/docs/architecture-decisions.md).

## Current state

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
- **Verification:** 872 backend tests, 119 frontend tests; ruff/format clean;
  frontend typecheck/build clean; fresh DB applies **37 migrations**.
- **mypy:** 90 pre-existing errors across `cli.py`, `paper_performance.py`,
  `infra/scheduler.py` and the tunnel test fakes. Do not “fix” them as part of
  maintenance; the tunnel change adds none.
- **Next migration number:** `039` if a new migration is needed.

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

# campaign (first run creates paper-main, saves defaults, seeds schedules)
uv run --locked bq campaign init
uv run --locked bq campaign refresh-master
uv run --locked bq campaign research
uv run --locked bq campaign scan
uv run --locked bq campaign status
uv run --locked bq campaign review
```
