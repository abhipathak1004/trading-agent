-- Editable, versioned agent prompts (Agent Lab).
--
-- A save appends a new version; it never rewrites an earlier one. The prompt
-- text is configuration, not a financial record, but keeping every version makes
-- a refinement reviewable and lets calibration be tied to the prompt in force.
-- There is no DELETE: "reset" writes the default text as a new version.

CREATE TABLE agent_prompts (
    prompt_id TEXT PRIMARY KEY NOT NULL CHECK (length(prompt_id) BETWEEN 1 AND 128),
    agent_id TEXT NOT NULL CHECK (length(agent_id) BETWEEN 1 AND 64),
    version INTEGER NOT NULL CHECK (version >= 1),
    system TEXT NOT NULL CHECK (length(system) BETWEEN 1 AND 16384),
    template TEXT NOT NULL CHECK (length(template) <= 16384),
    updated_at TEXT NOT NULL,
    prompt_hash TEXT NOT NULL CHECK (length(prompt_hash) = 64),
    UNIQUE (agent_id, version)
) STRICT;

CREATE INDEX agent_prompts_by_agent ON agent_prompts (agent_id, version DESC);

CREATE TRIGGER agent_prompts_no_update
BEFORE UPDATE ON agent_prompts
BEGIN
    SELECT RAISE(ABORT, 'agent prompts are immutable');
END;

CREATE TRIGGER agent_prompts_no_delete
BEFORE DELETE ON agent_prompts
BEGIN
    SELECT RAISE(ABORT, 'agent prompts cannot be deleted');
END;
