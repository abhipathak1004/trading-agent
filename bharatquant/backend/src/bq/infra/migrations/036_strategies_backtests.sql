-- Versioned strategy definitions, backtest runs and owner reviews.

CREATE TABLE strategy_versions (
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('RULE', 'LLM_DISCRETIONARY', 'HYBRID')),
    lifecycle TEXT NOT NULL CHECK (
        lifecycle IN ('DRAFT', 'BACKTESTED', 'PAPER_SHADOW', 'PAPER_ACTIVE')
    ),
    definition_json TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    is_seed INTEGER NOT NULL DEFAULT 0 CHECK (is_seed IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (strategy_id, version)
) STRICT;

CREATE TABLE backtest_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    engine TEXT NOT NULL CHECK (engine IN ('FAST', 'EVENT')),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    initial_capital_paise INTEGER NOT NULL CHECK (initial_capital_paise > 0),
    metrics_json TEXT NOT NULL,
    trades_json TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    created_at TEXT NOT NULL,
    FOREIGN KEY (strategy_id, version)
        REFERENCES strategy_versions (strategy_id, version)
) STRICT;

CREATE INDEX backtest_runs_by_strategy
ON backtest_runs (strategy_id, version, created_at);

CREATE TABLE strategy_reviews (
    review_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id),
    decision TEXT NOT NULL CHECK (decision IN ('ACCEPTED', 'REJECTED')),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX strategy_reviews_by_run
ON strategy_reviews (run_id, created_at);

CREATE TRIGGER strategy_versions_no_update
BEFORE UPDATE ON strategy_versions
BEGIN
    SELECT RAISE(ABORT, 'strategy versions are immutable');
END;

CREATE TRIGGER strategy_versions_no_delete
BEFORE DELETE ON strategy_versions
BEGIN
    SELECT RAISE(ABORT, 'strategy versions cannot be deleted');
END;

CREATE TRIGGER backtest_runs_no_update
BEFORE UPDATE ON backtest_runs
BEGIN
    SELECT RAISE(ABORT, 'backtest runs are immutable');
END;

CREATE TRIGGER backtest_runs_no_delete
BEFORE DELETE ON backtest_runs
BEGIN
    SELECT RAISE(ABORT, 'backtest runs cannot be deleted');
END;

CREATE TRIGGER strategy_reviews_no_update
BEFORE UPDATE ON strategy_reviews
BEGIN
    SELECT RAISE(ABORT, 'strategy reviews are append-only');
END;

CREATE TRIGGER strategy_reviews_no_delete
BEFORE DELETE ON strategy_reviews
BEGIN
    SELECT RAISE(ABORT, 'strategy reviews cannot be deleted');
END;
