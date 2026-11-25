-- Immutable filing metadata and valuation/thesis foundations.

CREATE TABLE filings (
    filing_id TEXT PRIMARY KEY NOT NULL CHECK (length(filing_id) BETWEEN 1 AND 128),
    instrument_id TEXT NOT NULL,
    filing_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    filing_hash TEXT NOT NULL CHECK (length(filing_hash) = 64)
) STRICT;

CREATE TABLE theses (
    thesis_id TEXT PRIMARY KEY NOT NULL CHECK (length(thesis_id) BETWEEN 1 AND 128),
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK (horizon IN ('SHORT_TERM','SWING','MEDIUM','LONG')),
    state TEXT NOT NULL CHECK (state IN ('DRAFT','ACTIVE','INVALIDATED','ARCHIVED')),
    created_at TEXT NOT NULL,
    thesis_hash TEXT NOT NULL CHECK (length(thesis_hash) = 64)
) STRICT;

CREATE TRIGGER filings_no_update
BEFORE UPDATE ON filings
BEGIN
    SELECT RAISE(ABORT, 'filings are immutable');
END;

CREATE TRIGGER theses_no_update
BEFORE UPDATE ON theses
BEGIN
    SELECT RAISE(ABORT, 'theses are immutable');
END;
