CREATE TABLE calendar_sessions (
    session_id TEXT PRIMARY KEY NOT NULL,
    exchange_code TEXT NOT NULL CHECK (length(exchange_code) BETWEEN 1 AND 16),
    session_date TEXT NOT NULL CHECK (length(session_date) = 10),
    session_class TEXT NOT NULL CHECK (
        session_class IN ('REGULAR', 'SPECIAL', 'HOLIDAY')
    ),
    state TEXT NOT NULL CHECK (state IN ('OPEN', 'CLOSED')),
    opens_at TEXT,
    closes_at TEXT,
    source_url TEXT NOT NULL CHECK (substr(source_url, 1, 8) = 'https://'),
    source_published_at TEXT,
    known_at TEXT NOT NULL,
    reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
    reviewed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (exchange_code, session_date, known_at),
    CHECK (
        (state = 'OPEN' AND opens_at IS NOT NULL AND closes_at IS NOT NULL
            AND opens_at < closes_at)
        OR
        (state = 'CLOSED' AND opens_at IS NULL AND closes_at IS NULL)
    ),
    CHECK (session_class != 'HOLIDAY' OR state = 'CLOSED')
) STRICT;

CREATE INDEX calendar_sessions_as_of
ON calendar_sessions (exchange_code, session_date, known_at DESC, reviewed_at);

CREATE TRIGGER calendar_sessions_no_update
BEFORE UPDATE ON calendar_sessions
BEGIN
    SELECT RAISE(ABORT, 'calendar sessions are append-only');
END;

CREATE TRIGGER calendar_sessions_no_delete
BEFORE DELETE ON calendar_sessions
BEGIN
    SELECT RAISE(ABORT, 'calendar sessions are append-only');
END;

CREATE TRIGGER calendar_sessions_no_replace
BEFORE INSERT ON calendar_sessions
WHEN EXISTS (
    SELECT 1 FROM calendar_sessions
    WHERE session_id = NEW.session_id
       OR (
            exchange_code = NEW.exchange_code
            AND session_date = NEW.session_date
            AND known_at = NEW.known_at
       )
)
BEGIN
    SELECT RAISE(ABORT, 'calendar sessions are append-only');
END;
