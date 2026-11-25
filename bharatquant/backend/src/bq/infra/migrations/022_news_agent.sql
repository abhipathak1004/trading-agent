-- News items ingested from an external feed, plus agent memory.
--
-- Cost design: every item stores only a compact dedupe form (title, short
-- description, hashes, category). Full article text is never fetched or stored.
-- Long-term-impact memory is a separate, smaller table so retrieval stays cheap.

CREATE TABLE news_items (
    news_id TEXT PRIMARY KEY NOT NULL CHECK (length(news_id) BETWEEN 1 AND 128),
    source_id TEXT NOT NULL CHECK (length(source_id) BETWEEN 1 AND 128),
    url TEXT NOT NULL CHECK (length(url) BETWEEN 1 AND 2048),
    url_hash TEXT NOT NULL CHECK (length(url_hash) = 64),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 512),
    summary TEXT NOT NULL CHECK (length(summary) BETWEEN 0 AND 1024),
    shingle_hash TEXT NOT NULL CHECK (length(shingle_hash) = 64),
    category TEXT NOT NULL CHECK (
        category IN (
            'INDIA_MARKET', 'GLOBAL_MARKET', 'GEOPOLITICAL', 'COMMODITY',
            'RATES', 'CORPORATE', 'REGULATORY', 'OTHER'
        )
    ),
    long_term_impact INTEGER NOT NULL CHECK (long_term_impact IN (0, 1)),
    published_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    item_hash TEXT NOT NULL CHECK (length(item_hash) = 64),
    UNIQUE (url_hash)
) STRICT;

CREATE INDEX news_items_by_published
ON news_items (published_at DESC, news_id);

CREATE INDEX news_items_by_shingle
ON news_items (shingle_hash);

CREATE INDEX news_items_long_term
ON news_items (long_term_impact, published_at DESC);

-- Agent memory: only items the deterministic rules marked as long-term impact.
CREATE TABLE agent_memories (
    memory_id TEXT PRIMARY KEY NOT NULL CHECK (length(memory_id) BETWEEN 1 AND 128),
    agent_id TEXT NOT NULL CHECK (length(agent_id) BETWEEN 1 AND 128),
    kind TEXT NOT NULL CHECK (
        kind IN ('NEWS_LONG_TERM', 'NEWS_DEDUPE', 'REFLECTION')
    ),
    news_id TEXT REFERENCES news_items(news_id),
    category TEXT NOT NULL CHECK (
        category IN (
            'INDIA_MARKET', 'GLOBAL_MARKET', 'GEOPOLITICAL', 'COMMODITY',
            'RATES', 'CORPORATE', 'REGULATORY', 'OTHER'
        )
    ),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 512),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 0 AND 2048),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    known_at TEXT NOT NULL,
    expires_at TEXT,
    hit_count INTEGER NOT NULL DEFAULT 0 CHECK (hit_count >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (agent_id, kind, content_hash)
) STRICT;

CREATE INDEX agent_memories_by_agent
ON agent_memories (agent_id, kind, known_at DESC);

CREATE TRIGGER news_items_no_update
BEFORE UPDATE ON news_items
BEGIN
    SELECT RAISE(ABORT, 'news items are immutable');
END;

CREATE TRIGGER news_items_no_delete
BEFORE DELETE ON news_items
BEGIN
    SELECT RAISE(ABORT, 'news items cannot be deleted');
END;

CREATE TRIGGER agent_memories_no_delete
BEFORE DELETE ON agent_memories
BEGIN
    SELECT RAISE(ABORT, 'agent memories cannot be deleted');
END;
