import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.memory import SemanticDoc, SemanticError, SemanticMemoryStore

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.instant = NOW

    def now(self) -> datetime:
        return self.instant


def _store(
    tmp_path: Path, *, fts: bool | None = None
) -> tuple[SemanticMemoryStore, MutableClock]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = MutableClock()
    return SemanticMemoryStore(path, clock, fts=fts), clock


def _ingest(
    store: SemanticMemoryStore,
    *,
    symbol: str,
    title: str,
    body: str,
    published_at: datetime = NOW - timedelta(days=2),
    known_at: datetime | None = None,
    url: str = "https://example.com/doc",
) -> SemanticDoc:
    return store.ingest(
        symbol=symbol,
        source_url=url,
        title=title,
        body=body,
        published_at=published_at,
        known_at=known_at,
        metadata={"source": "test"},
    )


def test_semantic_search_supports_fts_and_the_term_fallback(tmp_path: Path) -> None:
    fts_store, _ = _store(tmp_path / "fts")
    fallback_store, _ = _store(tmp_path / "fallback", fts=False)
    for store in (fts_store, fallback_store):
        _ingest(
            store,
            symbol="NSE:RELIANCE",
            title="Reliance breakout",
            body="Reliance cleared resistance on strong volume.",
            url="https://example.com/reliance",
        )
        _ingest(
            store,
            symbol="NSE:TCS",
            title="TCS dividend",
            body="TCS announced a dividend and record date.",
            url="https://example.com/tcs",
        )
    for store in (fts_store, fallback_store):
        hits = store.search("breakout resistance", as_of=NOW)
        assert [hit.document.symbol for hit in hits] == ["NSE:RELIANCE"]
        assert hits[0].score > 0
        assert store.search("dividend", symbol="NSE:RELIANCE", as_of=NOW) == ()
    assert fts_store.fts_available is True
    assert fallback_store.fts_available is False


def test_semantic_memory_is_point_in_time(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    _ingest(
        store,
        symbol="NSE:RELIANCE",
        title="Reliance breakout",
        body="breakout on volume",
        published_at=NOW - timedelta(days=1),
        known_at=NOW,
        url="https://example.com/reliance",
    )
    assert store.search("breakout", as_of=NOW - timedelta(minutes=1)) == ()
    assert len(store.search("breakout", as_of=NOW)) == 1
    clock.instant = NOW + timedelta(days=2)
    _ingest(
        store,
        symbol="NSE:RELIANCE",
        title="Reliance future guidance",
        body="guidance raised",
        published_at=NOW + timedelta(days=1),
        url="https://example.com/future",
    )
    assert len(store.search("guidance", as_of=NOW)) == 0
    assert len(store.search("guidance", as_of=NOW + timedelta(days=3))) == 1


def test_semantic_documents_are_immutable_and_bounded(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    doc = _ingest(
        store,
        symbol="NSE:RELIANCE",
        title="Reliance breakout",
        body="breakout on volume",
        url="https://example.com/reliance",
    )
    assert doc.doc_id.startswith("sd_")
    with pytest.raises(SemanticError, match="HTTPS"):
        _ingest(
            store,
            symbol="NSE:X",
            title="bad",
            body="bad",
            url="http://insecure.example/x",
        )
    with closing(sqlite3.connect(tmp_path / "bq.db")) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE semantic_docs SET title = 'x' WHERE doc_id = ?",
                (doc.doc_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM semantic_docs WHERE doc_id = ?", (doc.doc_id,)
            )
