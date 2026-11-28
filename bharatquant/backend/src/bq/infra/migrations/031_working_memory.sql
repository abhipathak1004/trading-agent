-- Append-only working memory for one analysed symbol in a run.
-- A Strategy run may contain several symbols, so the key is (run_id, symbol).

CREATE TABLE working_memory (
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('analysis', 'strategy')),
    as_of TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, symbol)
) STRICT;

CREATE INDEX working_memory_by_symbol
ON working_memory (symbol, as_of);

CREATE TRIGGER working_memory_no_update
BEFORE UPDATE ON working_memory
BEGIN
    SELECT RAISE(ABORT, 'working memory cannot be updated');
END;

CREATE TRIGGER working_memory_no_delete
BEFORE DELETE ON working_memory
BEGIN
    SELECT RAISE(ABORT, 'working memory cannot be deleted');
END;
