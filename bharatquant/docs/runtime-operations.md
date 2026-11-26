# Runtime operations

Phase 19 hardening. Everything here is local and paper-only.

## Start and wake

```sh
cd bharatquant
BQ_HOME="$HOME/Library/Application Support/BharatQuant" uv run --locked bq serve
```

`bq serve` starts the scheduler and the single job worker in the same process. Do
not start a second `bq serve` against the same database: the job store uses leases,
but two workers are unnecessary. On macOS, keep the machine awake only while the
market session needs it:

```sh
caffeinate -dimsu -w $$  # tie to the shell that owns bq serve
```

Sleep/wake is handled by the scheduler's wake policy and the job store's lease
reclaim: a missed occurrence is not back-filled with hindsight; one queued
occurrence remains durable and a stale lease is reclaimed. Check the runtime page
for `stale` jobs after a long sleep.

### launchd example

Save as `~/Library/LaunchAgents/com.bharatquant.serve.plist`; replace the paths and
keep `BQ_HOME` explicit. `RunAtLoad` starts on login, `KeepAlive` restarts after a
crash.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.bharatquant.serve</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>BQ_HOME=/Users/you/Library/Application Support/BharatQuant</string>
    <string>/path/to/bharatquant/.venv/bin/bq</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/bharatquant.log</string>
  <key>StandardErrorPath</key><string>/tmp/bharatquant.err.log</string>
</dict>
</plist>
```

## Health and metrics

- `GET /api/v1/system/health` — public liveness probe.
- `GET /api/v1/system/status` — owner-only database/WAL sizes, migration count,
  job leases, stale jobs, table counts and issues.
- `GET /api/v1/system/metrics` — owner-only Prometheus text.
- `WS /api/v1/events` — owner-authenticated runtime snapshots every 15 seconds or
  on change. It is a bounded operational feed, not a second job queue.

The Today page Runtime card polls the status endpoint every 30 seconds.

## Backup and restore

```sh
# verified backup + JSON manifest (sha256, bytes, migration count)
uv run --locked bq db backup --output "$HOME/Backups/bq-2026-11-26.db"

# restore is destructive and requires --confirm; the current DB is copied to
# bq.db.pre-restore first, and the restored file is integrity-checked.
uv run --locked bq db restore --backup "$HOME/Backups/bq-2026-11-26.db" --confirm
```

Backups use SQLite's online backup API, so WAL state is included. Keep backups
outside `BQ_HOME`; the app never deletes them.

## Benchmarks and checks

```sh
uv run --locked bq bench
uv run --locked bq doctor
cd bharatquant && PYTHONPATH=backend/src uv run --locked pytest -q
```

`bq bench` is a smoke benchmark of canonical hashing and timestamp parsing, not a
trading performance claim. Engine parity is enforced by the backtest tests.

## Failure policy

- A failed handler is a durable failed job. It does not crash the worker loop.
- A stale running lease is reclaimed when possible; exhausted attempts are marked
  `FAILED` with `lease_expired`.
- A database integrity or migration error makes the Runtime status `degraded` and
  should stop operational work until repaired.
- No hardening path enables a live broker or bypasses paper-only execution.
