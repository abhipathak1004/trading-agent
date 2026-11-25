-- Frozen paper-research predictions and append-only resolutions.

CREATE TABLE predictions (
    prediction_id TEXT PRIMARY KEY NOT NULL CHECK (length(prediction_id) BETWEEN 1 AND 128),
    created_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    probability TEXT NOT NULL,
    positive_threshold TEXT,
    prompt_hash TEXT,
    manifest_hash TEXT,
    prediction_hash TEXT NOT NULL CHECK (length(prediction_hash) = 64)
) STRICT;

CREATE TABLE prediction_outcomes (
    prediction_id TEXT PRIMARY KEY NOT NULL REFERENCES predictions(prediction_id),
    resolved_at TEXT NOT NULL,
    occurred INTEGER NOT NULL CHECK (occurred IN (0, 1)),
    evidence_source_id TEXT,
    evidence_record_id TEXT,
    outcome_hash TEXT NOT NULL CHECK (length(outcome_hash) = 64)
) STRICT;

CREATE TRIGGER predictions_no_update
BEFORE UPDATE ON predictions
BEGIN
    SELECT RAISE(ABORT, 'predictions are immutable');
END;

CREATE TRIGGER predictions_no_delete
BEFORE DELETE ON predictions
BEGIN
    SELECT RAISE(ABORT, 'predictions cannot be deleted');
END;

CREATE TRIGGER prediction_outcomes_no_update
BEFORE UPDATE ON prediction_outcomes
BEGIN
    SELECT RAISE(ABORT, 'prediction outcomes are append-only');
END;

CREATE TRIGGER prediction_outcomes_no_delete
BEFORE DELETE ON prediction_outcomes
BEGIN
    SELECT RAISE(ABORT, 'prediction outcomes cannot be deleted');
END;
