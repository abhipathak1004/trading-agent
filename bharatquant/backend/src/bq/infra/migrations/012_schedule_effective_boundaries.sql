CREATE TABLE schedule_effective_boundaries (
    version INTEGER PRIMARY KEY REFERENCES schedule_versions(version),
    effective_at TEXT NOT NULL UNIQUE
) STRICT;

CREATE TRIGGER schedule_boundaries_no_update
BEFORE UPDATE ON schedule_effective_boundaries
BEGIN
    SELECT RAISE(ABORT, 'schedule boundaries are append-only');
END;

CREATE TRIGGER schedule_boundaries_no_delete
BEFORE DELETE ON schedule_effective_boundaries
BEGIN
    SELECT RAISE(ABORT, 'schedule boundaries are append-only');
END;

CREATE TRIGGER schedule_boundaries_no_replace
BEFORE INSERT ON schedule_effective_boundaries
WHEN EXISTS (
    SELECT 1 FROM schedule_effective_boundaries
    WHERE version = NEW.version OR effective_at = NEW.effective_at
)
BEGIN
    SELECT RAISE(ABORT, 'schedule boundaries are append-only');
END;
