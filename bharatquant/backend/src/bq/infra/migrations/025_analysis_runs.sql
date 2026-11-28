-- Append-only audit record of one analysis run.
--
-- Why this exists: a technical recommendation that cannot be reconstructed is
-- worthless for review, and the plan requires run metadata (vendors used, whether
-- data was real-time, indicator snapshot, debate rounds, tokens, decision hash) to
-- be retained for every run. The row is immutable: a correction is a new run.
--
-- `facts_sha256` fingerprints the deterministic facts so a later reviewer can tell
-- whether a plan was produced from the same inputs.

CREATE TABLE analysis_runs (
    run_id TEXT PRIMARY KEY NOT NULL CHECK (length(run_id) BETWEEN 1 AND 128),
    symbol TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 96),
    timeframe TEXT NOT NULL CHECK (timeframe IN ('intraday', 'swing')),
    as_of TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    vendor_quotes TEXT NOT NULL CHECK (length(vendor_quotes) <= 64),
    vendor_daily TEXT NOT NULL CHECK (length(vendor_daily) <= 64),
    is_realtime INTEGER NOT NULL CHECK (is_realtime IN (0, 1)),
    data_freshness TEXT NOT NULL CHECK (
        data_freshness IN ('realtime_kite', 'delayed_yfinance')
    ),
    bar_count INTEGER NOT NULL CHECK (bar_count >= 0),
    facts_sha256 TEXT NOT NULL CHECK (length(facts_sha256) = 64),
    composite REAL,
    debate_attempted INTEGER NOT NULL CHECK (debate_attempted IN (0, 1)),
    debate_rounds INTEGER NOT NULL CHECK (debate_rounds >= 0),
    mode TEXT NOT NULL CHECK (mode IN ('FULL', 'RULES_ONLY')),
    provider TEXT NOT NULL CHECK (length(provider) <= 64),
    model TEXT NOT NULL CHECK (length(model) <= 200),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    signal TEXT NOT NULL CHECK (signal IN ('BUY', 'SELL', 'HOLD', 'NONE')),
    plan_json TEXT CHECK (plan_json IS NULL OR length(plan_json) <= 8192),
    plan_sha256 TEXT CHECK (plan_sha256 IS NULL OR length(plan_sha256) = 64),
    vetoes_json TEXT NOT NULL CHECK (length(vetoes_json) <= 2048),
    detail_json TEXT NOT NULL CHECK (length(detail_json) <= 65536)
) STRICT;

CREATE INDEX analysis_runs_by_symbol
ON analysis_runs (symbol, as_of DESC);

CREATE TRIGGER analysis_runs_no_update
BEFORE UPDATE ON analysis_runs
BEGIN
    SELECT RAISE(ABORT, 'analysis runs are immutable');
END;

CREATE TRIGGER analysis_runs_no_delete
BEFORE DELETE ON analysis_runs
BEGIN
    SELECT RAISE(ABORT, 'analysis runs cannot be deleted');
END;
