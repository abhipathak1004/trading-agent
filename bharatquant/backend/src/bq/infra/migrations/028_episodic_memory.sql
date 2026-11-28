-- Episodic memory: decisions and shadow trades, with append-only outcomes.
--
-- The run-time record is immutable: a decision is a fact about what the system
-- believed at as_of. Resolution is a separate append-only row, so a later
-- outcome can never rewrite the original decision and Brier scoring can be
-- filtered to what was known at a chosen instant.

CREATE TABLE episodic_memory (
    episode_id TEXT PRIMARY KEY NOT NULL
        CHECK (length(episode_id) BETWEEN 1 AND 128),
    run_id TEXT CHECK (run_id IS NULL OR length(run_id) <= 128),
    symbol TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 64),
    as_of TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('analysis', 'strategy', 'manual')),
    decision_source TEXT NOT NULL CHECK (decision_source IN (
        'jev_native', 'llm_fallback', 'heuristic', 'rules_only',
        'llm_direct', 'rules')),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 200),
    regime TEXT CHECK (regime IS NULL OR length(regime) <= 32),
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL', 'HOLD', 'AVOID', 'SKIP')),
    status TEXT NOT NULL CHECK (status IN ('SHADOW', 'PROPOSED', 'EXECUTED')),
    entry_price_paise INTEGER CHECK (
        entry_price_paise IS NULL OR entry_price_paise > 0),
    stop_loss_paise INTEGER CHECK (
        stop_loss_paise IS NULL OR stop_loss_paise > 0),
    target_price_paise INTEGER CHECK (
        target_price_paise IS NULL OR target_price_paise > 0),
    quantity INTEGER CHECK (quantity IS NULL OR quantity > 0),
    predicted_probability TEXT CHECK (
        predicted_probability IS NULL OR length(predicted_probability) <= 32),
    detail_json TEXT NOT NULL CHECK (length(detail_json) <= 16384)
) STRICT;

CREATE INDEX episodic_memory_by_symbol ON episodic_memory (symbol, as_of DESC);
CREATE INDEX episodic_memory_by_source ON episodic_memory (decision_source, model);
CREATE INDEX episodic_memory_by_time ON episodic_memory (as_of DESC, episode_id);

CREATE TRIGGER episodic_memory_no_update
BEFORE UPDATE ON episodic_memory
BEGIN
    SELECT RAISE(ABORT, 'episodic memory is immutable');
END;

CREATE TRIGGER episodic_memory_no_delete
BEFORE DELETE ON episodic_memory
BEGIN
    SELECT RAISE(ABORT, 'episodic memory cannot be deleted');
END;

CREATE TABLE episode_outcomes (
    episode_id TEXT PRIMARY KEY NOT NULL
        REFERENCES episodic_memory(episode_id),
    resolved_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    outcome INTEGER NOT NULL CHECK (outcome IN (0, 1)),
    realized_r TEXT NOT NULL CHECK (length(realized_r) <= 32),
    close_price_paise INTEGER CHECK (
        close_price_paise IS NULL OR close_price_paise > 0),
    source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 64)
) STRICT;

CREATE INDEX episode_outcomes_by_time ON episode_outcomes (resolved_at, episode_id);

CREATE TRIGGER episode_outcomes_no_update
BEFORE UPDATE ON episode_outcomes
BEGIN
    SELECT RAISE(ABORT, 'episode outcomes are immutable');
END;

CREATE TRIGGER episode_outcomes_no_delete
BEFORE DELETE ON episode_outcomes
BEGIN
    SELECT RAISE(ABORT, 'episode outcomes cannot be deleted');
END;

CREATE TRIGGER episode_outcomes_no_replace
BEFORE INSERT ON episode_outcomes
WHEN EXISTS (SELECT 1 FROM episode_outcomes WHERE episode_id = NEW.episode_id)
BEGIN
    SELECT RAISE(ABORT, 'episode outcome already resolved');
END;
