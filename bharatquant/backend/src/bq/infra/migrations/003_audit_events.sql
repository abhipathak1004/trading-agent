CREATE TABLE audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (length(actor) > 0),
    action TEXT NOT NULL CHECK (length(action) > 0),
    object_type TEXT NOT NULL CHECK (length(object_type) > 0),
    object_id TEXT NOT NULL CHECK (length(object_id) > 0),
    config_version TEXT,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    previous_hash TEXT CHECK (previous_hash IS NULL OR length(previous_hash) = 64),
    event_hash TEXT NOT NULL UNIQUE CHECK (length(event_hash) = 64)
) STRICT;

CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

CREATE TRIGGER audit_events_no_replace
BEFORE INSERT ON audit_events
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE sequence = NEW.sequence
       OR event_id = NEW.event_id
       OR event_hash = NEW.event_hash
)
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;
