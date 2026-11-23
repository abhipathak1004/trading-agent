CREATE TABLE job_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL CHECK (length(job_id) BETWEEN 1 AND 128),
    occurrence_key TEXT NOT NULL CHECK (length(occurrence_key) BETWEEN 1 AND 128),
    scheduled_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    lease_owner TEXT,
    lease_token TEXT,
    lease_until TEXT,
    failure_code TEXT,
    UNIQUE (job_id, occurrence_key),
    CHECK (attempts <= max_attempts),
    CHECK (
        (status = 'RUNNING' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL
            AND lease_until IS NOT NULL)
        OR
        (status != 'RUNNING' AND lease_owner IS NULL AND lease_token IS NULL
            AND lease_until IS NULL)
    )
) STRICT;

CREATE INDEX job_runs_claimable
ON job_runs (status, available_at, lease_until, scheduled_at);

CREATE TRIGGER job_runs_no_delete
BEFORE DELETE ON job_runs
BEGIN
    SELECT RAISE(ABORT, 'job run identities cannot be deleted');
END;

CREATE TABLE job_run_events (
    sequence INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES job_runs(run_id),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'ENQUEUED', 'CLAIMED', 'LEASE_RECLAIMED', 'RETRY_SCHEDULED',
            'SUCCEEDED', 'FAILED', 'LEASE_EXPIRED'
        )
    ),
    occurred_at TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 0),
    worker_id TEXT,
    failure_code TEXT
) STRICT;

CREATE INDEX job_run_events_by_run
ON job_run_events (run_id, sequence);

CREATE TRIGGER job_run_events_no_update
BEFORE UPDATE ON job_run_events
BEGIN
    SELECT RAISE(ABORT, 'job run events are append-only');
END;

CREATE TRIGGER job_run_events_no_delete
BEFORE DELETE ON job_run_events
BEGIN
    SELECT RAISE(ABORT, 'job run events are append-only');
END;

CREATE TRIGGER job_run_events_no_replace
BEFORE INSERT ON job_run_events
WHEN EXISTS (
    SELECT 1 FROM job_run_events WHERE sequence = NEW.sequence
)
BEGIN
    SELECT RAISE(ABORT, 'job run events are append-only');
END;
