-- Durable history for the interactive research agent loop. Each run is one
-- `POST /api/v1/agent/run`, and each step is one observable tool action in that
-- loop. Rows are append-only, matching the discipline of analysis runs and the
-- audit chain.

CREATE TABLE agent_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    agent_id TEXT NOT NULL,
    task TEXT NOT NULL,
    final_text TEXT NOT NULL,
    turns INTEGER NOT NULL CHECK (turns >= 0),
    stopped INTEGER NOT NULL CHECK (stopped IN (0, 1)),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
) STRICT;

CREATE INDEX agent_runs_by_finished
ON agent_runs (finished_at DESC);

CREATE TABLE agent_run_steps (
    run_id TEXT NOT NULL REFERENCES agent_runs (run_id),
    turn INTEGER NOT NULL CHECK (turn >= 1),
    kind TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    output TEXT NOT NULL,
    PRIMARY KEY (run_id, turn)
) STRICT;

CREATE INDEX agent_run_steps_by_run
ON agent_run_steps (run_id, turn);

CREATE TRIGGER agent_runs_no_delete
BEFORE DELETE ON agent_runs
BEGIN
    SELECT RAISE(ABORT, 'agent runs are append-only');
END;

CREATE TRIGGER agent_run_steps_no_delete
BEFORE DELETE ON agent_run_steps
BEGIN
    SELECT RAISE(ABORT, 'agent run steps are append-only');
END;
