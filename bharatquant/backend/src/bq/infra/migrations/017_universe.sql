-- Owner-supplied universe rules and point-in-time membership.

CREATE TABLE universe_versions (
    universe_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    effective_date TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    minimum_market_cap_paise INTEGER NOT NULL CHECK (minimum_market_cap_paise >= 0),
    source_id TEXT NOT NULL,
    source_url TEXT,
    PRIMARY KEY (universe_id, version)
) STRICT;

CREATE TABLE universe_membership (
    universe_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    instrument_id TEXT NOT NULL,
    market_cap_paise INTEGER NOT NULL CHECK (market_cap_paise >= 0),
    PRIMARY KEY (universe_id, version, instrument_id),
    FOREIGN KEY (universe_id, version)
        REFERENCES universe_versions(universe_id, version)
) STRICT;

CREATE TRIGGER universe_versions_no_delete
BEFORE DELETE ON universe_versions
BEGIN
    SELECT RAISE(ABORT, 'universe versions cannot be deleted');
END;

CREATE TRIGGER universe_membership_no_delete
BEFORE DELETE ON universe_membership
BEGIN
    SELECT RAISE(ABORT, 'universe membership cannot be deleted');
END;
