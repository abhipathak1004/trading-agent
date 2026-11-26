-- Semantic memory: immutable research documents plus a deterministic term index.
--
-- The FTS5 index is a derived search cache created by SemanticMemoryStore when
-- the runtime supports FTS5. The authoritative documents and the term index are
-- created here, so search still works on a SQLite build without FTS5.

CREATE TABLE semantic_docs (
    doc_id TEXT PRIMARY KEY NOT NULL CHECK (length(doc_id) BETWEEN 1 AND 128),
    symbol TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 64),
    source_url TEXT NOT NULL CHECK (length(source_url) BETWEEN 1 AND 2048),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 512),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 16384),
    published_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    metadata_json TEXT NOT NULL CHECK (length(metadata_json) <= 4096)
) STRICT;

CREATE INDEX semantic_docs_by_symbol ON semantic_docs (symbol, published_at DESC);
CREATE INDEX semantic_docs_by_known ON semantic_docs (known_at, recorded_at);
CREATE INDEX semantic_docs_by_published ON semantic_docs (published_at DESC, doc_id);

CREATE TRIGGER semantic_docs_no_update
BEFORE UPDATE ON semantic_docs
BEGIN
    SELECT RAISE(ABORT, 'semantic documents are immutable');
END;

CREATE TRIGGER semantic_docs_no_delete
BEFORE DELETE ON semantic_docs
BEGIN
    SELECT RAISE(ABORT, 'semantic documents cannot be deleted');
END;

CREATE TABLE semantic_terms (
    doc_id TEXT NOT NULL REFERENCES semantic_docs(doc_id),
    term TEXT NOT NULL CHECK (length(term) BETWEEN 2 AND 64),
    frequency INTEGER NOT NULL CHECK (frequency > 0),
    PRIMARY KEY (doc_id, term)
) STRICT;

CREATE INDEX semantic_terms_by_term ON semantic_terms (term, doc_id);

CREATE TRIGGER semantic_terms_no_update
BEFORE UPDATE ON semantic_terms
BEGIN
    SELECT RAISE(ABORT, 'semantic terms are immutable');
END;

CREATE TRIGGER semantic_terms_no_delete
BEFORE DELETE ON semantic_terms
BEGIN
    SELECT RAISE(ABORT, 'semantic terms cannot be deleted');
END;
