CREATE TABLE data_imports (
    import_id TEXT PRIMARY KEY NOT NULL,
    import_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    source_id TEXT NOT NULL,
    review_url TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    reviewed_by TEXT NOT NULL CHECK (reviewed_by = 'owner'),
    recorded_at TEXT NOT NULL,
    record_count INTEGER NOT NULL CHECK (record_count > 0)
) STRICT;

CREATE TABLE data_import_records (
    import_id TEXT NOT NULL REFERENCES data_imports(import_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    record_id TEXT NOT NULL UNIQUE REFERENCES point_in_time_records(record_id),
    PRIMARY KEY (import_id, ordinal)
) STRICT;

CREATE TRIGGER data_imports_no_update
BEFORE UPDATE ON data_imports
BEGIN
    SELECT RAISE(ABORT, 'data imports are append-only');
END;

CREATE TRIGGER data_imports_no_delete
BEFORE DELETE ON data_imports
BEGIN
    SELECT RAISE(ABORT, 'data imports are append-only');
END;

CREATE TRIGGER data_imports_no_replace
BEFORE INSERT ON data_imports
WHEN EXISTS (
    SELECT 1 FROM data_imports
    WHERE import_id = NEW.import_id OR import_key = NEW.import_key
)
BEGIN
    SELECT RAISE(ABORT, 'data imports are append-only');
END;

CREATE TRIGGER data_import_records_no_update
BEFORE UPDATE ON data_import_records
BEGIN
    SELECT RAISE(ABORT, 'data import records are append-only');
END;

CREATE TRIGGER data_import_records_no_delete
BEFORE DELETE ON data_import_records
BEGIN
    SELECT RAISE(ABORT, 'data import records are append-only');
END;

CREATE TRIGGER data_import_records_no_replace
BEFORE INSERT ON data_import_records
WHEN EXISTS (
    SELECT 1 FROM data_import_records
    WHERE (import_id = NEW.import_id AND ordinal = NEW.ordinal)
       OR record_id = NEW.record_id
)
BEGIN
    SELECT RAISE(ABORT, 'data import records are append-only');
END;
