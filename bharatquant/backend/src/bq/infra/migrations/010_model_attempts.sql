CREATE TABLE model_attempts (
    attempt_id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL,
    task_version TEXT NOT NULL,
    model_id TEXT NOT NULL,
    manifest_hash TEXT NOT NULL REFERENCES context_manifests(manifest_hash),
    started_at TEXT NOT NULL
) STRICT;

CREATE TABLE model_attempt_outcomes (
    attempt_id TEXT PRIMARY KEY NOT NULL REFERENCES model_attempts(attempt_id),
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'SUCCEEDED', 'PROVIDER_ERROR', 'TIMEOUT', 'INVALID_RESPONSE', 'INTERRUPTED', 'CACHE_HIT'
    )),
    usage_kind TEXT NOT NULL CHECK (usage_kind IN ('REPORTED', 'ESTIMATED', 'UNKNOWN', 'NO_CALL')),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    failure_code TEXT,
    CHECK (
        (usage_kind = 'UNKNOWN' AND input_tokens IS NULL AND output_tokens IS NULL)
        OR (usage_kind IN ('REPORTED', 'ESTIMATED')
            AND (input_tokens IS NOT NULL OR output_tokens IS NOT NULL))
        OR (usage_kind = 'NO_CALL' AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL
            AND input_tokens = 0 AND output_tokens = 0)
    ),
    CHECK ((status = 'CACHE_HIT' AND usage_kind = 'NO_CALL')
        OR (status != 'CACHE_HIT' AND usage_kind != 'NO_CALL')),
    CHECK ((status IN ('SUCCEEDED', 'CACHE_HIT') AND failure_code IS NULL)
        OR (status NOT IN ('SUCCEEDED', 'CACHE_HIT') AND failure_code IS NOT NULL))
) STRICT;

CREATE TRIGGER model_attempts_no_update
BEFORE UPDATE ON model_attempts
BEGIN
    SELECT RAISE(ABORT, 'model attempts are append-only');
END;

CREATE TRIGGER model_attempts_no_delete
BEFORE DELETE ON model_attempts
BEGIN
    SELECT RAISE(ABORT, 'model attempts are append-only');
END;

CREATE TRIGGER model_attempts_no_replace
BEFORE INSERT ON model_attempts
WHEN EXISTS (SELECT 1 FROM model_attempts WHERE attempt_id = NEW.attempt_id)
BEGIN
    SELECT RAISE(ABORT, 'model attempts are append-only');
END;

CREATE TRIGGER model_attempt_outcomes_no_update
BEFORE UPDATE ON model_attempt_outcomes
BEGIN
    SELECT RAISE(ABORT, 'model attempt outcomes are append-only');
END;

CREATE TRIGGER model_attempt_outcomes_no_delete
BEFORE DELETE ON model_attempt_outcomes
BEGIN
    SELECT RAISE(ABORT, 'model attempt outcomes are append-only');
END;

CREATE TRIGGER model_attempt_outcomes_no_replace
BEFORE INSERT ON model_attempt_outcomes
WHEN EXISTS (SELECT 1 FROM model_attempt_outcomes WHERE attempt_id = NEW.attempt_id)
BEGIN
    SELECT RAISE(ABORT, 'model attempt outcomes are append-only');
END;
