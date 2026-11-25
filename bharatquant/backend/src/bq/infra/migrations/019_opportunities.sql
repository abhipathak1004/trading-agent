-- Frozen opportunities and shadow-tracked confidence assessments.

CREATE TABLE opportunities (
    opportunity_id TEXT PRIMARY KEY NOT NULL CHECK (length(opportunity_id) BETWEEN 1 AND 128),
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK (horizon IN ('INTRADAY','SHORT_TERM','SWING','MEDIUM','LONG')),
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    created_at TEXT NOT NULL,
    probability TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (
        disposition IN ('SUGGEST','WATCH','DISCARD')
    ),
    level TEXT NOT NULL CHECK (
        level IN ('VERY_HIGH','HIGH','MODERATE','LOW')
    ),
    calibrated INTEGER NOT NULL CHECK (calibrated IN (0,1)),
    ev_r TEXT NOT NULL,
    assessment_hash TEXT NOT NULL CHECK (length(assessment_hash) = 64)
) STRICT;

CREATE TRIGGER opportunities_no_update
BEFORE UPDATE ON opportunities
BEGIN
    SELECT RAISE(ABORT, 'opportunities are immutable');
END;

CREATE TRIGGER opportunities_no_delete
BEFORE DELETE ON opportunities
BEGIN
    SELECT RAISE(ABORT, 'opportunities cannot be deleted');
END;
