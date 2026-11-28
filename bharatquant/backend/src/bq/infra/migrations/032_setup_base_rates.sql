-- Versioned, append-only setup base rates.
-- v1 computes a symbol-scoped trend-continuation study; the regime column is
-- present so a later market-wide study can fill it without a schema change.

CREATE TABLE setup_base_rates (
    rate_id TEXT PRIMARY KEY NOT NULL,
    setup_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    regime TEXT NOT NULL,
    horizon_days INTEGER NOT NULL CHECK (horizon_days > 0),
    sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
    win_count INTEGER NOT NULL CHECK (win_count >= 0 AND win_count <= sample_count),
    win_rate REAL NOT NULL CHECK (win_rate >= 0 AND win_rate <= 1),
    data_as_of TEXT NOT NULL,
    computed_at TEXT NOT NULL
) STRICT;

CREATE INDEX setup_base_rates_lookup
ON setup_base_rates (setup_id, symbol, regime, horizon_days, data_as_of);

CREATE TRIGGER setup_base_rates_no_update
BEFORE UPDATE ON setup_base_rates
BEGIN
    SELECT RAISE(ABORT, 'setup base rates cannot be updated');
END;

CREATE TRIGGER setup_base_rates_no_delete
BEFORE DELETE ON setup_base_rates
BEGIN
    SELECT RAISE(ABORT, 'setup base rates cannot be deleted');
END;
