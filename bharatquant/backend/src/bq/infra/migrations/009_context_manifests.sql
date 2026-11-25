CREATE TABLE context_manifests (
    manifest_hash TEXT PRIMARY KEY NOT NULL CHECK (length(manifest_hash) = 64),
    recorded_at TEXT NOT NULL,
    manifest_json TEXT NOT NULL
) STRICT;

CREATE TRIGGER context_manifests_no_update
BEFORE UPDATE ON context_manifests
BEGIN
    SELECT RAISE(ABORT, 'context manifests are append-only');
END;

CREATE TRIGGER context_manifests_no_delete
BEFORE DELETE ON context_manifests
BEGIN
    SELECT RAISE(ABORT, 'context manifests are append-only');
END;

CREATE TRIGGER context_manifests_no_replace
BEFORE INSERT ON context_manifests
WHEN EXISTS (SELECT 1 FROM context_manifests WHERE manifest_hash = NEW.manifest_hash)
BEGIN
    SELECT RAISE(ABORT, 'context manifests are append-only');
END;
