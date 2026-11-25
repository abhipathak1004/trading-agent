-- Immutable deterministic risk and approval decisions.

CREATE TABLE risk_decisions (
    decision_id TEXT PRIMARY KEY NOT NULL CHECK (length(decision_id) BETWEEN 1 AND 128),
    opportunity_id TEXT,
    occurred_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ALLOW','BLOCK')),
    vetoes_json TEXT NOT NULL,
    decision_hash TEXT NOT NULL CHECK (length(decision_hash) = 64)
) STRICT;

CREATE TABLE approval_decisions (
    decision_id TEXT PRIMARY KEY NOT NULL CHECK (length(decision_id) BETWEEN 1 AND 128),
    opportunity_id TEXT,
    occurred_at TEXT NOT NULL,
    effective_mode TEXT NOT NULL CHECK (
        effective_mode IN (
            'AUTO','APPROVE_ABOVE_THRESHOLD','APPROVE_ALL','SUGGEST_ONLY'
        )
    ),
    approved INTEGER NOT NULL CHECK (approved IN (0,1)),
    decision_hash TEXT NOT NULL CHECK (length(decision_hash) = 64)
) STRICT;

CREATE TRIGGER risk_decisions_no_update
BEFORE UPDATE ON risk_decisions
BEGIN
    SELECT RAISE(ABORT, 'risk decisions are immutable');
END;

CREATE TRIGGER approval_decisions_no_update
BEFORE UPDATE ON approval_decisions
BEGIN
    SELECT RAISE(ABORT, 'approval decisions are immutable');
END;
