CREATE TABLE schedule_wake_decisions (
    schedule_id TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    schedule_version INTEGER NOT NULL REFERENCES schedule_versions(version),
    decision TEXT NOT NULL CHECK (
        decision IN (
            'RUN', 'SKIPPED_BY_POLICY', 'WAKE_WINDOW_EXPIRED',
            'OUTSIDE_OPERATING_WINDOW'
        )
    ),
    PRIMARY KEY (schedule_id, scheduled_at)
) STRICT;

CREATE TRIGGER schedule_wake_decisions_no_update
BEFORE UPDATE ON schedule_wake_decisions
BEGIN
    SELECT RAISE(ABORT, 'schedule wake decisions are append-only');
END;

CREATE TRIGGER schedule_wake_decisions_no_delete
BEFORE DELETE ON schedule_wake_decisions
BEGIN
    SELECT RAISE(ABORT, 'schedule wake decisions are append-only');
END;

CREATE TRIGGER schedule_wake_decisions_no_replace
BEFORE INSERT ON schedule_wake_decisions
WHEN EXISTS (
    SELECT 1 FROM schedule_wake_decisions
    WHERE schedule_id = NEW.schedule_id AND scheduled_at = NEW.scheduled_at
)
BEGIN
    SELECT RAISE(ABORT, 'schedule wake decisions are append-only');
END;
