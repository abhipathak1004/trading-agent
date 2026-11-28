-- AI Hedge Fund data: company facts, an earnings-surprise history, and a
-- period label on fundamental snapshots (ADR-053).
--
-- The ported fund engine reads point-in-time fundamentals through the same
-- `fundamental_snapshots` store the rest of the app uses, so the store gains
-- the two things the engine needs and the app did not yet have:
--
--   * `period_label` — 'ttm' / 'quarter' / 'annual'. The engine asks the data
--     for trailing-twelve-month rows; the store did not label which a row was.
--   * a `YAHOO` source. The only keyless fundamentals feed available is Yahoo's
--     quoteSummary, and labelling those rows 'XBRL' would be a lie about where
--     the numbers came from.
--
-- SQLite cannot alter a CHECK constraint, so `fundamental_snapshots` is rebuilt
-- — the same copy/drop/rename as migration 041. Its immutability triggers are
-- dropped with the table and recreated below; no foreign key references it, so
-- every stored snapshot survives. The migration runner wraps this file in one
-- transaction.
--
-- `company_facts` exists because the engine's sector cap (`max_sector_pct`)
-- reads a sector per name and silently exempts a name with none — so without a
-- sector store that limit would appear configured and do nothing.
--
-- `earnings_events` exists because the deterministic PEAD analyst fires on a
-- BEAT/MISS that was *filed* on a date, and needs to know that date to stay
-- point-in-time. It is keyed by (symbol, report_period) and keeps the first
-- announcement seen.

-- ---------------------------------------------------------------------------
-- fundamental_snapshots: widen the source CHECK, add the period label
-- ---------------------------------------------------------------------------

CREATE TABLE fundamental_snapshots_new (
    snapshot_id TEXT PRIMARY KEY NOT NULL,
    instrument_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('XBRL', 'OWNER', 'YAHOO')),
    period_end TEXT NOT NULL,
    period_label TEXT CHECK (
        period_label IS NULL OR period_label IN ('ttm', 'quarter', 'annual')
    ),
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

-- Every pre-existing row keeps its values; only the new nullable label is added.
INSERT INTO fundamental_snapshots_new (
    snapshot_id, instrument_id, source, period_end, period_label, known_at,
    revenue_paise, ebitda_paise, net_income_paise, total_debt_paise, cash_paise,
    shares_outstanding, book_value_paise, fcf_paise, snapshot_hash, created_at
)
SELECT
    snapshot_id, instrument_id, source, period_end, NULL, known_at,
    revenue_paise, ebitda_paise, net_income_paise, total_debt_paise, cash_paise,
    shares_outstanding, book_value_paise, fcf_paise, snapshot_hash, created_at
FROM fundamental_snapshots;

DROP TRIGGER fundamental_snapshots_no_update;
DROP TRIGGER fundamental_snapshots_no_delete;
DROP INDEX fundamental_snapshots_by_instrument;
DROP TABLE fundamental_snapshots;

ALTER TABLE fundamental_snapshots_new RENAME TO fundamental_snapshots;

CREATE INDEX fundamental_snapshots_by_instrument
ON fundamental_snapshots (instrument_id, known_at, period_end);

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

-- ---------------------------------------------------------------------------
-- company_facts: the slow-moving company attributes a factor model reads
-- ---------------------------------------------------------------------------

CREATE TABLE company_facts (
    symbol TEXT PRIMARY KEY NOT NULL,
    company_name TEXT,
    isin TEXT,
    sector TEXT,
    industry TEXT,
    -- When this row became knowable. Sector/industry are treated as
    -- slow-moving (the documented point-in-time approximation), so a run reads
    -- the newest row whose known_at has passed rather than a per-date history.
    known_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    recorded_at TEXT NOT NULL
) STRICT;

CREATE INDEX company_facts_by_sector ON company_facts (sector);

-- ---------------------------------------------------------------------------
-- earnings_events: one filed quarterly surprise, keyed by period
-- ---------------------------------------------------------------------------

CREATE TABLE earnings_events (
    symbol TEXT NOT NULL,
    report_period TEXT NOT NULL,
    -- The date the result was announced/filed. This is the date the PEAD
    -- analyst compares against a session, so it must be the filing date and
    -- never the fiscal period end.
    filing_date TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'RESULT',
    eps_actual REAL,
    eps_estimate REAL,
    eps_surprise TEXT CHECK (eps_surprise IS NULL OR eps_surprise IN ('BEAT', 'MISS', 'MEET')),
    known_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (symbol, report_period, filing_date)
) STRICT;

CREATE INDEX earnings_events_by_symbol
ON earnings_events (symbol, filing_date);

CREATE TRIGGER earnings_events_no_delete
BEFORE DELETE ON earnings_events
BEGIN
    SELECT RAISE(ABORT, 'earnings events are append-only');
END;
