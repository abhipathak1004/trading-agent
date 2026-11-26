-- Opportunity lifecycle, confidence card detail and shadow trades.
-- The opportunities table stays immutable; lifecycle is an append-only event log.

ALTER TABLE opportunities ADD COLUMN run_id TEXT;
ALTER TABLE opportunities ADD COLUMN source TEXT;
ALTER TABLE opportunities ADD COLUMN setup_id TEXT;
ALTER TABLE opportunities ADD COLUMN confidence_json TEXT;
ALTER TABLE opportunities ADD COLUMN plan_json TEXT;

CREATE TABLE opportunity_events (
    event_id TEXT PRIMARY KEY NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    lifecycle TEXT NOT NULL CHECK (
        lifecycle IN (
            'NEW','WATCHING','TRIGGERED','TRADED','CLOSED','DISMISSED','EXPIRED'
        )
    ),
    occurred_at TEXT NOT NULL,
    note TEXT
) STRICT;

CREATE INDEX opportunity_events_by_opportunity
ON opportunity_events (opportunity_id, occurred_at, event_id);

CREATE TRIGGER opportunity_events_no_update
BEFORE UPDATE ON opportunity_events
BEGIN
    SELECT RAISE(ABORT, 'opportunity events cannot be updated');
END;

CREATE TRIGGER opportunity_events_no_delete
BEFORE DELETE ON opportunity_events
BEGIN
    SELECT RAISE(ABORT, 'opportunity events cannot be deleted');
END;

CREATE TABLE shadow_trades (
    shadow_id TEXT PRIMARY KEY NOT NULL,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    instrument_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    as_of TEXT NOT NULL,
    entry_price_paise INTEGER NOT NULL CHECK (entry_price_paise > 0),
    stop_loss_paise INTEGER NOT NULL CHECK (stop_loss_paise > 0),
    target_price_paise INTEGER NOT NULL CHECK (target_price_paise > 0),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    level TEXT NOT NULL,
    probability TEXT NOT NULL,
    suggested INTEGER NOT NULL CHECK (suggested IN (0,1)),
    not_taken_reason TEXT,
    plan_json TEXT NOT NULL,
    detail_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (opportunity_id)
) STRICT;

CREATE INDEX shadow_trades_by_as_of
ON shadow_trades (as_of, instrument_id);

CREATE TABLE shadow_outcomes (
    shadow_id TEXT NOT NULL REFERENCES shadow_trades(shadow_id),
    horizon_days INTEGER NOT NULL CHECK (horizon_days IN (1,3,5,10)),
    resolved_at TEXT NOT NULL,
    outcome INTEGER NOT NULL CHECK (outcome IN (0,1)),
    realized_r TEXT NOT NULL,
    close_price_paise INTEGER NOT NULL CHECK (close_price_paise > 0),
    source TEXT NOT NULL,
    PRIMARY KEY (shadow_id, horizon_days)
) STRICT;

CREATE TRIGGER shadow_outcomes_no_update
BEFORE UPDATE ON shadow_outcomes
BEGIN
    SELECT RAISE(ABORT, 'shadow outcomes cannot be updated');
END;

CREATE TRIGGER shadow_outcomes_no_delete
BEFORE DELETE ON shadow_outcomes
BEGIN
    SELECT RAISE(ABORT, 'shadow outcomes cannot be deleted');
END;
