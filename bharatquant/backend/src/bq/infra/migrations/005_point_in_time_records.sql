CREATE TABLE point_in_time_records (
    sequence INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL UNIQUE,
    record_kind TEXT NOT NULL CHECK (
        record_kind IN ('INSTRUMENT', 'EOD_BAR', 'SNAPSHOT', 'FILING', 'MARKET_STATISTIC')
    ),
    record_key TEXT NOT NULL CHECK (length(record_key) BETWEEN 1 AND 128),
    source_id TEXT NOT NULL CHECK (length(source_id) BETWEEN 1 AND 128),
    source_url TEXT NOT NULL CHECK (substr(source_url, 1, 8) = 'https://'),
    unit TEXT,
    event_at TEXT,
    published_at TEXT,
    known_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    quality_status TEXT NOT NULL CHECK (
        quality_status IN ('UNASSESSED', 'ACCEPTABLE', 'DEGRADED', 'REJECTED')
    ),
    payload_json TEXT NOT NULL CHECK (length(payload_json) > 1),
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    recorded_at TEXT NOT NULL
) STRICT;

CREATE INDEX point_in_time_records_as_of
ON point_in_time_records (record_kind, record_key, known_at DESC, sequence DESC);

CREATE TRIGGER point_in_time_records_no_update
BEFORE UPDATE ON point_in_time_records
BEGIN
    SELECT RAISE(ABORT, 'point-in-time records are append-only');
END;

CREATE TRIGGER point_in_time_records_no_delete
BEFORE DELETE ON point_in_time_records
BEGIN
    SELECT RAISE(ABORT, 'point-in-time records are append-only');
END;

CREATE TRIGGER point_in_time_records_no_replace
BEFORE INSERT ON point_in_time_records
WHEN EXISTS (
    SELECT 1 FROM point_in_time_records WHERE record_id = NEW.record_id
)
BEGIN
    SELECT RAISE(ABORT, 'point-in-time records are append-only');
END;
