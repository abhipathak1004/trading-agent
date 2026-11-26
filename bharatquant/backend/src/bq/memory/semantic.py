"""Semantic memory: point-in-time document search with FTS5 or a term fallback.

Documents and their deterministic term index are authoritative and immutable.
An FTS5 table is a derived cache created lazily when the runtime supports FTS5;
if it does not, the same search API uses the term index. Every query filters
``published_at``, ``known_at`` and ``recorded_at`` against the requested
``as_of`` so retrieval cannot see the future.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from bq.core import (
    Clock,
    SystemClock,
    canonical_digest,
    open_connection,
    parse_timestamp,
    timestamp,
    utc,
)
from bq.infra.audit import append_event

MAX_DOC_ID_CHARS = 128
MAX_SYMBOL_CHARS = 64
MAX_URL_CHARS = 2048
MAX_TITLE_CHARS = 512
MAX_BODY_CHARS = 16_384
MAX_METADATA_CHARS = 4_096
MAX_QUERY_CHARS = 256
MAX_TERMS = 256
MAX_QUERY_TERMS = 16
MAX_LIMIT = 50

_TERM = re.compile(r"[a-z0-9]{2,64}")


class SemanticError(ValueError):
    pass


class MissingSemanticDoc(SemanticError):
    pass


def _text(value: object, *, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise SemanticError(f"{label} must be text.")
    text = value.strip()
    if not 1 <= len(text) <= limit:
        raise SemanticError(f"{label} must be between 1 and {limit} characters.")
    return text


def _aware(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise SemanticError(f"{label} must be timezone-aware.")
    return utc(value)


def _source_url(value: object) -> str:
    url = _text(value, label="source_url", limit=MAX_URL_CHARS)
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise SemanticError("source_url must be HTTPS without credentials.")
    return url


def _terms(text: str) -> dict[str, int]:
    counts = Counter(_TERM.findall(text.lower()))
    return dict(counts.most_common(MAX_TERMS))


@dataclass(frozen=True, slots=True)
class SemanticDoc:
    doc_id: str
    symbol: str
    source_url: str
    title: str
    body: str
    published_at: datetime
    known_at: datetime
    recorded_at: datetime
    content_hash: str
    metadata: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "doc_id": self.doc_id,
            "symbol": self.symbol,
            "source_url": self.source_url,
            "title": self.title,
            "body": self.body,
            "published_at": self.published_at.isoformat(),
            "known_at": self.known_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "content_hash": self.content_hash,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class SemanticHit:
    document: SemanticDoc
    score: float

    def to_dict(self) -> dict[str, object]:
        return {"document": self.document.to_dict(), "score": self.score}


class SemanticMemoryStore:
    def __init__(
        self,
        path: Path,
        clock: Clock | None = None,
        *,
        fts: bool | None = None,
    ) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self._fts_setting = fts
        self._fts: bool | None = None

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    @property
    def fts_available(self) -> bool:
        return self._ensure_fts()

    def ingest(
        self,
        *,
        symbol: str,
        source_url: str,
        title: str,
        body: str,
        published_at: datetime,
        known_at: datetime | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> SemanticDoc:
        symbol_text = _text(symbol, label="symbol", limit=MAX_SYMBOL_CHARS)
        url = _source_url(source_url)
        title_text = _text(title, label="title", limit=MAX_TITLE_CHARS)
        body_text = _text(body, label="body", limit=MAX_BODY_CHARS)
        published = _aware(published_at, label="published_at")
        known = (
            utc(self.clock.now())
            if known_at is None
            else _aware(known_at, label="known_at")
        )
        recorded_at = utc(self.clock.now())
        if known < published:
            raise SemanticError("known_at cannot precede published_at.")
        if known > recorded_at:
            raise SemanticError("known_at cannot be in the future.")
        metadata_json = self._metadata_json(metadata)
        content_hash = sha256(f"{title_text}\n{body_text}".encode("utf-8")).hexdigest()
        doc_id = (
            "sd_"
            + canonical_digest(
                {
                    "symbol": symbol_text,
                    "source_url": url,
                    "published_at": published.isoformat(),
                    "content_hash": content_hash,
                }
            )[:32]
        )
        terms = _terms(f"{title_text} {body_text}")
        fts = self._ensure_fts()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT doc_id FROM semantic_docs WHERE doc_id = ?",
                    (doc_id,),
                ).fetchone()
                if existing is not None:
                    return self.get(doc_id)
                connection.execute(
                    "INSERT INTO semantic_docs ("
                    "doc_id, symbol, source_url, title, body, published_at, "
                    "known_at, recorded_at, content_hash, metadata_json"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        symbol_text,
                        url,
                        title_text,
                        body_text,
                        timestamp(published),
                        timestamp(known),
                        timestamp(recorded_at),
                        content_hash,
                        metadata_json,
                    ),
                )
                connection.executemany(
                    "INSERT INTO semantic_terms (doc_id, term, frequency) "
                    "VALUES (?, ?, ?)",
                    [(doc_id, term, count) for term, count in terms.items()],
                )
                if fts:
                    connection.execute(
                        "INSERT INTO semantic_docs_fts (doc_id, title, body) "
                        "VALUES (?, ?, ?)",
                        (doc_id, title_text, body_text),
                    )
                append_event(
                    connection,
                    occurred_at=recorded_at,
                    actor="system",
                    action="memory.semantic.recorded",
                    object_type="semantic_doc",
                    object_id=doc_id,
                    payload={"symbol": symbol_text},
                )
        return self.get(doc_id)

    def get(self, doc_id: str) -> SemanticDoc:
        identifier = _text(doc_id, label="doc_id", limit=MAX_DOC_ID_CHARS)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT doc_id, symbol, source_url, title, body, published_at, "
                "known_at, recorded_at, content_hash, metadata_json "
                "FROM semantic_docs WHERE doc_id = ?",
                (identifier,),
            ).fetchone()
        if row is None:
            raise MissingSemanticDoc("No such semantic document.")
        return self._document(row)

    def search(
        self,
        query: str,
        *,
        as_of: datetime | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> tuple[SemanticHit, ...]:
        query_text = _text(query, label="query", limit=MAX_QUERY_CHARS)
        terms = list(_terms(query_text))[:MAX_QUERY_TERMS]
        if not terms:
            return ()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIMIT
        ):
            raise SemanticError(f"limit must be between 1 and {MAX_LIMIT}.")
        pivot = utc(self.clock.now()) if as_of is None else _aware(as_of, label="as_of")
        symbol_text = (
            None
            if symbol is None
            else _text(symbol, label="symbol", limit=MAX_SYMBOL_CHARS)
        )
        clauses = [
            "d.published_at <= ?",
            "d.known_at <= ?",
            "d.recorded_at <= ?",
        ]
        params: list[object] = [timestamp(pivot), timestamp(pivot), timestamp(pivot)]
        if symbol_text is not None:
            clauses.append("d.symbol = ?")
            params.append(symbol_text)
        where = " AND ".join(clauses)
        if self._ensure_fts():
            try:
                hits = self._search_fts(terms, where, params, limit)
                if hits:
                    return hits
            except sqlite3.OperationalError:
                pass
        return self._search_terms(terms, where, params, limit)

    def documents(
        self,
        *,
        as_of: datetime | None = None,
        symbol: str | None = None,
        limit: int = 20,
    ) -> tuple[SemanticDoc, ...]:
        pivot = utc(self.clock.now()) if as_of is None else _aware(as_of, label="as_of")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIMIT
        ):
            raise SemanticError(f"limit must be between 1 and {MAX_LIMIT}.")
        clauses = ["published_at <= ?", "known_at <= ?", "recorded_at <= ?"]
        params: list[object] = [timestamp(pivot), timestamp(pivot), timestamp(pivot)]
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(_text(symbol, label="symbol", limit=MAX_SYMBOL_CHARS))
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT doc_id, symbol, source_url, title, body, published_at, "
                "known_at, recorded_at, content_hash, metadata_json "
                "FROM semantic_docs WHERE " + " AND ".join(clauses) + " "
                "ORDER BY published_at DESC, doc_id LIMIT ?",
                params,
            ).fetchall()
        return tuple(self._document(row) for row in rows)

    def _ensure_fts(self) -> bool:
        if self._fts is not None:
            return self._fts
        if self._fts_setting is False:
            self._fts = False
            return False
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'semantic_docs_fts'"
            ).fetchone()
            if existing is None:
                try:
                    connection.execute(
                        "CREATE VIRTUAL TABLE semantic_docs_fts USING fts5("
                        "doc_id UNINDEXED, title, body)"
                    )
                    connection.execute(
                        "INSERT INTO semantic_docs_fts (doc_id, title, body) "
                        "SELECT doc_id, title, body FROM semantic_docs"
                    )
                    connection.commit()
                except sqlite3.OperationalError:
                    self._fts = False
                    return False
            self._fts = True
        return True

    def _search_fts(
        self,
        terms: list[str],
        where: str,
        params: list[object],
        limit: int,
    ) -> tuple[SemanticHit, ...]:
        match = " OR ".join(f'"{term}"' for term in terms)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT d.doc_id, d.symbol, d.source_url, d.title, d.body, "
                "d.published_at, d.known_at, d.recorded_at, d.content_hash, "
                "d.metadata_json, -bm25(semantic_docs_fts) AS score "
                "FROM semantic_docs_fts "
                "JOIN semantic_docs AS d ON d.doc_id = semantic_docs_fts.doc_id "
                "WHERE semantic_docs_fts MATCH ? AND " + where + " "
                "ORDER BY bm25(semantic_docs_fts) ASC, d.published_at DESC "
                "LIMIT ?",
                (match, *params, limit),
            ).fetchall()
        return tuple(
            SemanticHit(document=self._document(row), score=float(str(row[10])))
            for row in rows
        )

    def _search_terms(
        self,
        terms: list[str],
        where: str,
        params: list[object],
        limit: int,
    ) -> tuple[SemanticHit, ...]:
        placeholders = ",".join("?" for _ in terms)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT d.doc_id, d.symbol, d.source_url, d.title, d.body, "
                "d.published_at, d.known_at, d.recorded_at, d.content_hash, "
                "d.metadata_json, SUM(t.frequency) AS score "
                "FROM semantic_terms AS t "
                "JOIN semantic_docs AS d ON d.doc_id = t.doc_id "
                f"WHERE t.term IN ({placeholders}) AND " + where + " "
                "GROUP BY d.doc_id "
                "ORDER BY score DESC, d.published_at DESC LIMIT ?",
                (*terms, *params, limit),
            ).fetchall()
        return tuple(
            SemanticHit(document=self._document(row), score=float(str(row[10])))
            for row in rows
        )

    @staticmethod
    def _metadata_json(metadata: Mapping[str, object] | None) -> str:
        payload = dict(metadata or {})
        try:
            text = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise SemanticError("metadata must be JSON-serializable.") from exc
        if len(text) > MAX_METADATA_CHARS:
            raise SemanticError("metadata is too large.")
        return text

    @staticmethod
    def _document(row: tuple[object, ...]) -> SemanticDoc:
        raw = str(row[9])
        metadata = json.loads(raw) if raw else {}
        if not isinstance(metadata, dict):
            raise SemanticError("Stored metadata is invalid.")
        return SemanticDoc(
            doc_id=str(row[0]),
            symbol=str(row[1]),
            source_url=str(row[2]),
            title=str(row[3]),
            body=str(row[4]),
            published_at=parse_timestamp(str(row[5])),
            known_at=parse_timestamp(str(row[6])),
            recorded_at=parse_timestamp(str(row[7])),
            content_hash=str(row[8]),
            metadata=metadata,
        )


__all__ = [
    "MAX_BODY_CHARS",
    "MAX_QUERY_CHARS",
    "MissingSemanticDoc",
    "SemanticDoc",
    "SemanticError",
    "SemanticHit",
    "SemanticMemoryStore",
]
