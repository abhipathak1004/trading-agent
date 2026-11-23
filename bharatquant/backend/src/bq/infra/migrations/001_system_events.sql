CREATE TABLE system_events (
    event_id TEXT PRIMARY KEY NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (length(kind) > 0)
) STRICT;
