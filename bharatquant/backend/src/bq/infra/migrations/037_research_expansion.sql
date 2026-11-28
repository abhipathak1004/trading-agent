-- Point-in-time research, fundamental snapshots, thesis evidence and claims.

CREATE TABLE research_notes (
    note_id TEXT PRIMARY KEY NOT NULL,
    instrument_id TEXT,
    source_type TEXT NOT NULL CHECK (
        source_type IN ('NEWS', 'YOUTUBE', 'XBRL', 'FILING', 'OWNER')
    ),
    source_id TEXT NOT NULL,
    url TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    published_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    note_hash TEXT NOT NULL CHECK (length(note_hash) = 64),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX research_notes_by_instrument
ON research_notes (instrument_id, known_at, published_at);

CREATE TABLE fundamental_snapshots (
    snapshot_id TEXT PRIMARY KEY NOT NULL,
    instrument_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('XBRL', 'OWNER')),
    period_end TEXT NOT NULL,
    known_at TEXT NOT NULL,
    revenue_paise INTEGER NOT NULL CHECK (revenue_paise >= 0),
    ebitda_paise INTEGER NOT NULL CHECK (ebitda_paise >= 0),
    net_income_paise INTEGER NOT NULL,
    total_debt_paise INTEGER NOT NULL CHECK (total_debt_paise >= 0),
    cash_paise INTEGER NOT NULL CHECK (cash_paise >= 0),
    shares_outstanding INTEGER NOT NULL CHECK (shares_outstanding > 0),
    book_value_paise INTEGER NOT NULL CHECK (book_value_paise > 0),
    fcf_paise INTEGER NOT NULL,
    snapshot_hash TEXT NOT NULL CHECK (length(snapshot_hash) = 64),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX fundamental_snapshots_by_instrument
ON fundamental_snapshots (instrument_id, known_at, period_end);

CREATE TABLE thesis_events (
    event_id TEXT PRIMARY KEY NOT NULL,
    thesis_id TEXT NOT NULL REFERENCES theses(thesis_id),
    kind TEXT NOT NULL CHECK (
        kind IN ('EVIDENCE', 'INVALIDATION', 'REACTIVATION', 'NOTE')
    ),
    statement TEXT NOT NULL,
    note_id TEXT REFERENCES research_notes(note_id),
    occurred_at TEXT NOT NULL,
    event_hash TEXT NOT NULL CHECK (length(event_hash) = 64),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX thesis_events_by_thesis
ON thesis_events (thesis_id, occurred_at);

CREATE TABLE research_claims (
    claim_id TEXT PRIMARY KEY NOT NULL,
    instrument_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    note_id TEXT REFERENCES research_notes(note_id),
    claim_hash TEXT NOT NULL CHECK (length(claim_hash) = 64),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX research_claims_by_instrument
ON research_claims (instrument_id, created_at);

CREATE TABLE research_claim_reviews (
    review_id TEXT PRIMARY KEY NOT NULL,
    claim_id TEXT NOT NULL REFERENCES research_claims(claim_id),
    decision TEXT NOT NULL CHECK (decision IN ('ACCEPTED', 'REJECTED')),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX research_claim_reviews_by_claim
ON research_claim_reviews (claim_id, created_at);

CREATE TRIGGER research_notes_no_update
BEFORE UPDATE ON research_notes
BEGIN
    SELECT RAISE(ABORT, 'research notes are immutable');
END;

CREATE TRIGGER research_notes_no_delete
BEFORE DELETE ON research_notes
BEGIN
    SELECT RAISE(ABORT, 'research notes cannot be deleted');
END;

CREATE TRIGGER fundamental_snapshots_no_update
BEFORE UPDATE ON fundamental_snapshots
BEGIN
    SELECT RAISE(ABORT, 'fundamental snapshots are immutable');
END;

CREATE TRIGGER fundamental_snapshots_no_delete
BEFORE DELETE ON fundamental_snapshots
BEGIN
    SELECT RAISE(ABORT, 'fundamental snapshots cannot be deleted');
END;

CREATE TRIGGER thesis_events_no_update
BEFORE UPDATE ON thesis_events
BEGIN
    SELECT RAISE(ABORT, 'thesis events are append-only');
END;

CREATE TRIGGER thesis_events_no_delete
BEFORE DELETE ON thesis_events
BEGIN
    SELECT RAISE(ABORT, 'thesis events cannot be deleted');
END;

CREATE TRIGGER research_claims_no_update
BEFORE UPDATE ON research_claims
BEGIN
    SELECT RAISE(ABORT, 'research claims are immutable');
END;

CREATE TRIGGER research_claims_no_delete
BEFORE DELETE ON research_claims
BEGIN
    SELECT RAISE(ABORT, 'research claims cannot be deleted');
END;

CREATE TRIGGER research_claim_reviews_no_update
BEFORE UPDATE ON research_claim_reviews
BEGIN
    SELECT RAISE(ABORT, 'research claim reviews are append-only');
END;

CREATE TRIGGER research_claim_reviews_no_delete
BEFORE DELETE ON research_claim_reviews
BEGIN
    SELECT RAISE(ABORT, 'research claim reviews cannot be deleted');
END;
