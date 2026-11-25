CREATE TABLE owner_credentials (
    owner_id INTEGER PRIMARY KEY CHECK (owner_id = 1),
    password_hash TEXT NOT NULL
) STRICT;

CREATE TABLE auth_throttle (
    owner_id INTEGER PRIMARY KEY CHECK (owner_id = 1),
    failures INTEGER NOT NULL CHECK (failures >= 0),
    locked_until INTEGER NOT NULL
) STRICT;

INSERT INTO auth_throttle VALUES (1, 0, 0);

CREATE TABLE auth_sessions (
    token_hash TEXT PRIMARY KEY NOT NULL,
    csrf_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL
) STRICT;
