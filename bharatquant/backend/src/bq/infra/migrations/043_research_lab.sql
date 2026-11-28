-- Research Lab persistence (ADR-054).
--
-- Six tables, following the corpus conventions: STRICT tables, TEXT ids holding
-- token_urlsafe values, microsecond ISO-8601 UTC timestamps, and paise as
-- non-negative INTEGERs. Amounts are paise because this project's money type is
-- CurrencyPaise and a float rupee column would reintroduce rounding drift.
--
-- Two deliberate omissions:
--
--   * There is no `audit_ledger` table. `audit_events` (003) is already a
--     hash-chained, append-only ledger verified on every `bq doctor`; a second
--     chain would be a second source of truth. Mandate and run actions append to
--     the existing chain through `bq.infra.audit.append_event`.
--   * `research_artifacts_fts` is NOT created here. Stock SQLite builds may lack
--     FTS5, so the virtual table is created lazily at runtime behind a
--     try/except with a deterministic LIKE fallback, exactly as
--     `semantic_docs_fts` already is (bq/memory/semantic.py).

CREATE TABLE research_sessions (
    session_id TEXT PRIMARY KEY NOT NULL,
    title TEXT NOT NULL,
    -- Per-session model overrides. Ephemeral previews live only in memory; this
    -- column stores an explicit "save for this session" only.
    model_overrides_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
) STRICT;

CREATE INDEX research_sessions_by_updated
ON research_sessions (updated_at_utc DESC);

CREATE TABLE research_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL REFERENCES research_sessions (session_id),
    -- A replay or walk-forward child records the run it derives from.
    parent_run_id TEXT REFERENCES research_runs (run_id),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'cancelled', 'failed', 'blocked')
    ),
    prompt TEXT NOT NULL,
    -- The resolved planner/synthesizer/auditor routes, so a run can be explained
    -- after the owner changes Settings.
    model_config_json TEXT NOT NULL,
    -- theta: universe, period, product, price convention, capital and costs.
    params_json TEXT NOT NULL,
    -- Content digest of the frozen inputs (not the file bytes; see freeze.py).
    data_manifest_sha256 TEXT CHECK (
        data_manifest_sha256 IS NULL OR length(data_manifest_sha256) = 64
    ),
    frozen_data_path TEXT,
    metrics_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    artifact_dir TEXT,
    created_at_utc TEXT NOT NULL,
    completed_at_utc TEXT
) STRICT;

CREATE INDEX research_runs_by_session
ON research_runs (session_id, created_at_utc DESC);

CREATE INDEX research_runs_by_status
ON research_runs (status, created_at_utc DESC);

CREATE INDEX research_runs_by_parent
ON research_runs (parent_run_id);

CREATE TABLE portfolio_snapshots (
    snapshot_id TEXT PRIMARY KEY NOT NULL,
    source TEXT NOT NULL,
    as_of_utc TEXT NOT NULL,
    holdings_json TEXT NOT NULL,
    -- A source that failed to refresh is recorded, never silently dropped from
    -- totals: `is_complete = 0` is the only honest report of a partial snapshot.
    failed_sources_json TEXT NOT NULL,
    is_complete INTEGER NOT NULL CHECK (is_complete IN (0, 1)),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at_utc TEXT NOT NULL
) STRICT;

CREATE INDEX portfolio_snapshots_by_as_of
ON portfolio_snapshots (as_of_utc DESC);

CREATE TABLE research_artifacts (
    artifact_id TEXT PRIMARY KEY NOT NULL,
    slug TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    -- NULL for revision 1; otherwise the revision this one replaces.
    parent_revision INTEGER,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    linked_run_ids_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (slug, revision)
) STRICT;

CREATE INDEX research_artifacts_by_slug
ON research_artifacts (slug, revision DESC);

-- Checker-admitted evidence E_t (arXiv:2611.24845 §3.2.3). A row is admissible
-- only when producer_kind is engine or tool; `model` rows can be written (so a
-- rejection is testable against a real row) but the ledger refuses them.
--
-- Not to be confused with `research_claims` (037), which reviews narrative
-- thesis statements against an instrument. This table holds *numeric* evidence
-- scoped to the run that produced it: (run_id, metric_key, value, unit).
CREATE TABLE research_evidence (
    run_id TEXT NOT NULL REFERENCES research_runs (run_id),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    tool_name TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    value_num REAL NOT NULL,
    unit TEXT NOT NULL CHECK (
        unit IN ('ratio', 'pct', 'bp', 'inr', 'paise', 'count', 'bars')
    ),
    time_basis TEXT NOT NULL CHECK (
        time_basis IN ('point', 'per_day', 'per_bar', 'annualized')
    ),
    producer_kind TEXT NOT NULL CHECK (producer_kind IN ('engine', 'tool', 'model')),
    source_ref TEXT NOT NULL,
    admitted_at_utc TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
) STRICT;

CREATE INDEX research_evidence_by_metric
ON research_evidence (run_id, metric_key);

-- Account-bound authority to act (paper only in this build). The execution layer
-- refuses anything whose broker is not PAPER; no live broker path exists
-- (ADR-001, ADR-013).
CREATE TABLE execution_mandates (
    mandate_id TEXT PRIMARY KEY NOT NULL,
    broker TEXT NOT NULL CHECK (broker IN ('PAPER')),
    account_id TEXT NOT NULL,
    allowed_symbols_json TEXT NOT NULL,
    max_order_notional_paise INTEGER NOT NULL CHECK (max_order_notional_paise >= 0),
    max_daily_exposure_paise INTEGER NOT NULL CHECK (max_daily_exposure_paise >= 0),
    expires_at_utc TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    created_at_utc TEXT NOT NULL
) STRICT;

CREATE INDEX execution_mandates_by_expiry
ON execution_mandates (expires_at_utc);

-- Runs transition status (queued -> running -> completed/cancelled), so they are
-- mutable but never deletable, matching `agent_runs`. Artifacts, snapshots and
-- evidence are immutable once written; a new artifact revision is a new row.
CREATE TRIGGER research_runs_no_delete
BEFORE DELETE ON research_runs
BEGIN
    SELECT RAISE(ABORT, 'research runs are append-only');
END;

CREATE TRIGGER research_sessions_no_delete
BEFORE DELETE ON research_sessions
BEGIN
    SELECT RAISE(ABORT, 'research sessions are append-only');
END;

CREATE TRIGGER research_artifacts_no_update
BEFORE UPDATE ON research_artifacts
BEGIN
    SELECT RAISE(ABORT, 'research artifact revisions are immutable');
END;

CREATE TRIGGER research_artifacts_no_delete
BEFORE DELETE ON research_artifacts
BEGIN
    SELECT RAISE(ABORT, 'research artifact revisions are immutable');
END;

CREATE TRIGGER portfolio_snapshots_no_update
BEFORE UPDATE ON portfolio_snapshots
BEGIN
    SELECT RAISE(ABORT, 'portfolio snapshots are immutable');
END;

CREATE TRIGGER portfolio_snapshots_no_delete
BEFORE DELETE ON portfolio_snapshots
BEGIN
    SELECT RAISE(ABORT, 'portfolio snapshots are immutable');
END;

CREATE TRIGGER research_evidence_no_update
BEFORE UPDATE ON research_evidence
BEGIN
    SELECT RAISE(ABORT, 'admitted evidence is immutable');
END;

CREATE TRIGGER research_evidence_no_delete
BEFORE DELETE ON research_evidence
BEGIN
    SELECT RAISE(ABORT, 'admitted evidence is immutable');
END;
