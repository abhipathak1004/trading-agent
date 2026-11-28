-- News-driven catalyst campaign: symbol master, catalyst records, daily shortlist.

-- NSE equity listing, versioned by the date it was fetched. Company names are
-- needed to attribute a headline ("Sun Pharmaceutical") to a ticker (SUNPHARMA).
CREATE TABLE equity_master (
    symbol TEXT NOT NULL,
    company_name TEXT NOT NULL,
    series TEXT NOT NULL,
    isin TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of_date)
) STRICT;

CREATE INDEX equity_master_by_date
ON equity_master (as_of_date, symbol);

-- One deterministic catalyst finding per (news item, symbol, event). Strength is
-- an integer 1..5 so the shortlist never depends on floating-point scoring.
CREATE TABLE news_catalysts (
    catalyst_id TEXT PRIMARY KEY NOT NULL,
    news_id TEXT NOT NULL REFERENCES news_items(news_id),
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    polarity INTEGER NOT NULL CHECK (polarity IN (-1, 0, 1)),
    strength INTEGER NOT NULL CHECK (strength BETWEEN 1 AND 5),
    matched_terms TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('DETERMINISTIC', 'LLM')),
    trade_date TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    UNIQUE (news_id, symbol, event_type)
) STRICT;

CREATE INDEX news_catalysts_by_day
ON news_catalysts (trade_date, symbol);

CREATE TABLE campaigns (
    campaign_id TEXT PRIMARY KEY NOT NULL,
    trade_date TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    as_of TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'RUNNING', 'READY', 'ACTIVE', 'CLOSED', 'CANCELLED', 'FAILED'
        )
    ),
    summary_json TEXT NOT NULL
) STRICT;

CREATE TABLE campaign_items (
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    symbol TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK (rank >= 1),
    catalyst_score REAL NOT NULL,
    events_json TEXT NOT NULL,
    analysis_run_id TEXT,
    verdict TEXT NOT NULL,
    plan_json TEXT,
    trigger_json TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'WATCHING', 'TRIGGERED', 'FILLED', 'MISSED_CHASE', 'INVALIDATED',
            'EXPIRED', 'BLOCKED', 'CANCELLED', 'CLOSED'
        )
    ),
    outcome_json TEXT,
    r_multiple REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (campaign_id, symbol)
) STRICT;

CREATE INDEX campaign_items_by_status
ON campaign_items (campaign_id, status, rank);

CREATE TABLE campaign_item_events (
    event_id TEXT PRIMARY KEY NOT NULL,
    campaign_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    FOREIGN KEY (campaign_id, symbol)
        REFERENCES campaign_items(campaign_id, symbol)
) STRICT;

CREATE INDEX campaign_item_events_by_item
ON campaign_item_events (campaign_id, symbol, at);

CREATE TRIGGER equity_master_no_update
BEFORE UPDATE ON equity_master
BEGIN
    SELECT RAISE(ABORT, 'equity master rows are immutable');
END;

CREATE TRIGGER equity_master_no_delete
BEFORE DELETE ON equity_master
BEGIN
    SELECT RAISE(ABORT, 'equity master rows cannot be deleted');
END;

CREATE TRIGGER news_catalysts_no_update
BEFORE UPDATE ON news_catalysts
BEGIN
    SELECT RAISE(ABORT, 'news catalysts are immutable');
END;

CREATE TRIGGER news_catalysts_no_delete
BEFORE DELETE ON news_catalysts
BEGIN
    SELECT RAISE(ABORT, 'news catalysts cannot be deleted');
END;

CREATE TRIGGER campaign_item_events_no_update
BEFORE UPDATE ON campaign_item_events
BEGIN
    SELECT RAISE(ABORT, 'campaign item events are append-only');
END;

CREATE TRIGGER campaign_item_events_no_delete
BEFORE DELETE ON campaign_item_events
BEGIN
    SELECT RAISE(ABORT, 'campaign item events cannot be deleted');
END;
