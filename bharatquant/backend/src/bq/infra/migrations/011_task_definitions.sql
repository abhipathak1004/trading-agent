CREATE TABLE task_definitions (
    task_id TEXT NOT NULL,
    task_version TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    definition_hash TEXT NOT NULL CHECK (length(definition_hash) = 64),
    PRIMARY KEY (task_id, task_version)
) STRICT;

CREATE TRIGGER task_definitions_no_update
BEFORE UPDATE ON task_definitions
BEGIN
    SELECT RAISE(ABORT, 'task definitions are append-only');
END;

CREATE TRIGGER task_definitions_no_delete
BEFORE DELETE ON task_definitions
BEGIN
    SELECT RAISE(ABORT, 'task definitions are append-only');
END;

CREATE TRIGGER task_definitions_no_replace
BEFORE INSERT ON task_definitions
WHEN EXISTS (
    SELECT 1 FROM task_definitions
    WHERE task_id = NEW.task_id AND task_version = NEW.task_version
)
BEGIN
    SELECT RAISE(ABORT, 'task definitions are append-only');
END;
