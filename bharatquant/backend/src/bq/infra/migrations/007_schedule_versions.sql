CREATE TABLE schedule_versions (
    version INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL CHECK (length(snapshot_hash) = 64)
) STRICT;

CREATE TRIGGER schedule_versions_no_update
BEFORE UPDATE ON schedule_versions
BEGIN
    SELECT RAISE(ABORT, 'schedule versions are append-only');
END;

CREATE TRIGGER schedule_versions_no_delete
BEFORE DELETE ON schedule_versions
BEGIN
    SELECT RAISE(ABORT, 'schedule versions are append-only');
END;

CREATE TRIGGER schedule_versions_no_replace
BEFORE INSERT ON schedule_versions
WHEN EXISTS (SELECT 1 FROM schedule_versions WHERE version = NEW.version)
BEGIN
    SELECT RAISE(ABORT, 'schedule versions are append-only');
END;
