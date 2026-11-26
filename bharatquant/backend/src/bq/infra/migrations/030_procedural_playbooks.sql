-- Procedural memory: versioned playbooks for strategy selection.
--
-- A playbook is owner-editable guidance, not policy. It can shape the Super
-- Agent's ranking prose; thresholds, sizing and risk remain code-owned. Every
-- save appends a version so a refinement is reviewable.

CREATE TABLE procedural_playbooks (
    playbook_id TEXT NOT NULL CHECK (length(playbook_id) BETWEEN 1 AND 64),
    version INTEGER NOT NULL CHECK (version >= 1),
    content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 16384),
    updated_at TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    PRIMARY KEY (playbook_id, version)
) STRICT;

CREATE INDEX procedural_playbooks_by_id
    ON procedural_playbooks (playbook_id, version DESC);

CREATE TRIGGER procedural_playbooks_no_update
BEFORE UPDATE ON procedural_playbooks
BEGIN
    SELECT RAISE(ABORT, 'procedural playbooks are immutable');
END;

CREATE TRIGGER procedural_playbooks_no_delete
BEFORE DELETE ON procedural_playbooks
BEGIN
    SELECT RAISE(ABORT, 'procedural playbooks cannot be deleted');
END;
