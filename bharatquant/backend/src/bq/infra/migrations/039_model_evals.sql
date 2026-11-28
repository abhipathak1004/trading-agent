-- Model Evals: one row per benchmark sweep, plus the per-model scorecard it
-- produced. Promptfoo runs as a single sequential process (one local LLM in
-- 24 GB unified memory at a time); `eval_runs_single_active` is the database
-- lock that makes a second active run impossible, so the API can answer 409
-- without a race.

CREATE TABLE eval_runs (
    id TEXT PRIMARY KEY NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
    ),
    models_json TEXT NOT NULL,
    categories_json TEXT NOT NULL,
    config_path TEXT NOT NULL,
    results_json_path TEXT,
    error_message TEXT
) STRICT;

CREATE UNIQUE INDEX eval_runs_single_active
ON eval_runs (status)
WHERE status IN ('queued', 'running');

CREATE INDEX eval_runs_by_created
ON eval_runs (created_at DESC);

CREATE TABLE eval_model_scores (
    id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES eval_runs (id),
    model TEXT NOT NULL,
    total_cases INTEGER NOT NULL CHECK (total_cases >= 0),
    overall_pass_rate REAL NOT NULL CHECK (
        overall_pass_rate BETWEEN 0.0 AND 1.0
    ),
    accuracy_pass_rate REAL NOT NULL CHECK (
        accuracy_pass_rate BETWEEN 0.0 AND 1.0
    ),
    json_parse_rate REAL,
    p50_ms REAL,
    p95_ms REAL,
    max_ms REAL,
    brier_score REAL,
    ece_5bin REAL,
    brier_n INTEGER NOT NULL CHECK (brier_n >= 0),
    category_rates_json TEXT NOT NULL,
    UNIQUE (run_id, model)
) STRICT;

CREATE INDEX eval_model_scores_by_run
ON eval_model_scores (run_id, model);

CREATE TRIGGER eval_runs_no_delete
BEFORE DELETE ON eval_runs
BEGIN
    SELECT RAISE(ABORT, 'eval runs are append-only');
END;

CREATE TRIGGER eval_model_scores_no_delete
BEFORE DELETE ON eval_model_scores
BEGIN
    SELECT RAISE(ABORT, 'eval model scores are append-only');
END;
