-- Scheduler checkpoints: durable high-water marks for the scheduler runner.
--
-- Each row records the latest `dispatched_through` instant for one named
-- scheduler instance.  The runner advances this mark only after successfully
-- completing a dispatch pass, so crashes or restarts re-process the last
-- open window rather than losing it.
--
-- Constraints:
-- * `scheduler_id` is a human-readable identifier for the scheduler instance
--   (e.g. "default").  At most one row per ID.
-- * `dispatched_through` is an ISO-8601 UTC timestamp (microsecond precision,
--   "Z" suffix) up to and including which all due occurrences have been
--   discovered and dispatched.  It never moves backwards.
-- * `recorded_at` is the wall-clock time the row was last written.
-- * Rows are never deleted; updates are the only mutation permitted.
-- * The initial row is inserted by the first `run_due` call.

CREATE TABLE scheduler_checkpoints (
    scheduler_id      TEXT NOT NULL PRIMARY KEY
                      CHECK (length(scheduler_id) BETWEEN 1 AND 128),
    dispatched_through TEXT NOT NULL,
    recorded_at       TEXT NOT NULL
) STRICT;

CREATE TRIGGER scheduler_checkpoints_no_delete
BEFORE DELETE ON scheduler_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'scheduler checkpoints cannot be deleted');
END;
