-- Immutable daily reports and prediction metadata.

CREATE TABLE reports (
    report_id TEXT PRIMARY KEY NOT NULL,
    report_type TEXT NOT NULL CHECK (report_type IN ('morning','evening')),
    report_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ON_TIME','LATE','FAILED')),
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    body_json TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    file_path TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX reports_by_date
ON reports (report_date, report_type, created_at);

CREATE TRIGGER reports_no_update
BEFORE UPDATE ON reports
BEGIN
    SELECT RAISE(ABORT, 'reports are immutable');
END;

CREATE TRIGGER reports_no_delete
BEFORE DELETE ON reports
BEGIN
    SELECT RAISE(ABORT, 'reports cannot be deleted');
END;

CREATE TABLE prediction_details (
    prediction_id TEXT PRIMARY KEY NOT NULL REFERENCES predictions(prediction_id),
    report_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('DIRECTION','RANGE','GAP','VOLATILITY','STANCE')
    ),
    target TEXT NOT NULL,
    statement TEXT NOT NULL,
    params_json TEXT NOT NULL,
    resolution_rule TEXT NOT NULL,
    model_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TRIGGER prediction_details_no_update
BEFORE UPDATE ON prediction_details
BEGIN
    SELECT RAISE(ABORT, 'prediction details are immutable');
END;

CREATE TRIGGER prediction_details_no_delete
BEFORE DELETE ON prediction_details
BEGIN
    SELECT RAISE(ABORT, 'prediction details cannot be deleted');
END;
