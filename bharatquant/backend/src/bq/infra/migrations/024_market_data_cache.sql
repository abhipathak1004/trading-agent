-- Read-only Kite Connect support: credentials, instrument master, OHLCV cache.
--
-- Why three tables and not one:
--
-- * `broker_credentials` holds broker secrets. It is deliberately separate from
--   `provider_api_keys`, because Kite is a broker, not an LLM provider, and must
--   not appear in the provider list or be selectable as a model route.
-- * `instrument_master` caches Kite's exchange dump (tens of thousands of rows)
--   once per trading day. Re-downloading it per call would be wasteful and rate
--   limited.
-- * `ohlcv_cache` keeps recent bars so an analysis run does not depend on a
--   vendor being reachable at that instant.
--
-- ADR-013: Kite is used read-only. There is no order table here, and none may be
-- added without superseding ADR-001.

CREATE TABLE broker_credentials (
    broker TEXT PRIMARY KEY NOT NULL CHECK (broker IN ('KITE')),
    api_key TEXT NOT NULL CHECK (length(api_key) BETWEEN 8 AND 512),
    api_secret TEXT NOT NULL CHECK (length(api_secret) BETWEEN 8 AND 512),
    access_token TEXT NOT NULL CHECK (length(access_token) BETWEEN 8 AND 512),
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE instrument_master (
    as_of_date TEXT NOT NULL CHECK (
        length(as_of_date) = 10 AND substr(as_of_date, 5, 1) = '-'
        AND substr(as_of_date, 8, 1) = '-'
    ),
    exchange TEXT NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    tradingsymbol TEXT NOT NULL CHECK (
        length(tradingsymbol) BETWEEN 1 AND 48
    ),
    instrument_token INTEGER NOT NULL CHECK (instrument_token > 0),
    lot_size INTEGER NOT NULL CHECK (lot_size > 0),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (as_of_date, exchange, tradingsymbol)
) STRICT;

CREATE INDEX instrument_master_lookup
ON instrument_master (exchange, tradingsymbol, as_of_date DESC);

CREATE TABLE ohlcv_cache (
    source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 64),
    symbol TEXT NOT NULL CHECK (
        length(symbol) BETWEEN 1 AND 96 AND substr(symbol, 1, 1) <> '=' AND
        substr(symbol, 1, 1) <> '-'
    ),
    interval TEXT NOT NULL CHECK (
        interval IN ('1m', '5m', '15m', '60m', '1d')
    ),
    bar_time TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume INTEGER NOT NULL CHECK (volume >= 0),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, symbol, interval, bar_time)
) STRICT;

CREATE INDEX ohlcv_cache_by_symbol
ON ohlcv_cache (symbol, interval, bar_time DESC);
