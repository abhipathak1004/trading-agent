import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate
from bq.news import (
    MAX_SUMMARY_CHARS,
    NewsCategory,
    NewsError,
    NewsStore,
    classify,
    normalized_url,
    parse_feed,
    shingle_hash,
    url_hash,
)
from bq.news_agent import NewsResearchAgent

NOW = datetime(2026, 11, 25, 22, 0, tzinfo=UTC)

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Pulse</title>
  <item>
    <title>RBI holds repo rate, flags inflation risk</title>
    <description>The monetary policy committee kept the policy rate unchanged.</description>
    <link>https://example.com/rbi-policy?utm=1</link>
    <pubDate>Wed, 25 Nov 2026 20:00:00 +0530</pubDate>
  </item>
  <item>
    <title>Nifty closes at record high as FII flows turn positive</title>
    <description>Sensex and Nifty gained on strong buying.</description>
    <link>https://example.com/nifty-record</link>
    <pubDate>Wed, 25 Nov 2026 19:00:00 +0530</pubDate>
  </item>
  <item>
    <title>Wipro wins large contract from BP</title>
    <description>Around 3,000 employees transition to the firm.</description>
    <link>https://example.com/wipro-bp</link>
    <pubDate>Wed, 25 Nov 2026 18:00:00 +0530</pubDate>
  </item>
  <item>
    <title>Ceasefire talks resume between major powers</title>
    <description>Diplomats meet to discuss sanctions and tariffs.</description>
    <link>https://example.com/ceasefire</link>
    <pubDate>Wed, 25 Nov 2026 17:00:00 +0530</pubDate>
  </item>
</channel></rss>
"""


class FrozenClock:
    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def _store(tmp_path: Path) -> NewsStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return NewsStore(path, FrozenClock())


def test_parse_feed_keeps_only_bounded_metadata(tmp_path: Path) -> None:
    entries = parse_feed(FEED, ingested_at=NOW)
    assert len(entries) == 4
    first = entries[0]
    assert first.url == "https://example.com/rbi-policy?utm=1"
    assert first.published_at.utcoffset() is not None
    assert len(first.title) <= 512


def test_parse_feed_rejects_oversized_and_invalid_payloads() -> None:
    with pytest.raises(NewsError, match="size limit"):
        parse_feed(b"x" * (3 * 1024 * 1024), ingested_at=NOW)
    with pytest.raises(NewsError, match="valid XML"):
        parse_feed(b"not xml", ingested_at=NOW)
    with pytest.raises(NewsError, match="timezone-aware"):
        parse_feed(FEED, ingested_at=datetime(2026, 11, 25))


def test_parse_feed_bounds_item_count_and_skips_incomplete_items() -> None:
    entries = parse_feed(FEED, ingested_at=NOW, max_items=2)
    assert len(entries) == 2
    fragment = b"<rss><channel><item><title>no link</title></item></channel></rss>"
    assert parse_feed(fragment, ingested_at=NOW) == ()


def test_parse_feed_does_not_trust_future_publication_times() -> None:
    future = b"""<rss><channel><item>
        <title>Future story</title>
        <link>https://example.com/future</link>
        <pubDate>Wed, 25 Nov 2037 20:00:00 +0530</pubDate>
    </item></channel></rss>"""
    entry = parse_feed(future, ingested_at=NOW)[0]
    assert entry.published_at == NOW


def test_urls_must_be_https_and_are_normalized() -> None:
    assert normalized_url("HTTPS://Example.COM/a/b#frag") == "https://example.com/a/b"
    with pytest.raises(NewsError, match="HTTPS"):
        normalized_url("http://example.com/a")
    assert url_hash("https://example.com/a") == url_hash("https://EXAMPLE.com/a#x")


def test_shingle_hash_ignores_word_order_but_not_content() -> None:
    assert shingle_hash("Nifty hits record high") == shingle_hash("record high Nifty hits")
    assert shingle_hash("Nifty hits record high") != shingle_hash("Sensex falls sharply")
    with pytest.raises(NewsError, match="no usable words"):
        shingle_hash("   ...   ")


@pytest.mark.parametrize(
    ("title", "expected", "long_term"),
    [
        ("RBI cuts repo rate by 25 bps", NewsCategory.RATES, True),
        ("RBI brings back 60 tonnes of gold reserves", NewsCategory.RATES, False),
        ("SEBI fines broker for insider trading", NewsCategory.REGULATORY, True),
        ("New sanctions announced after border conflict", NewsCategory.GEOPOLITICAL, True),
        ("Crude oil slips on demand worries", NewsCategory.COMMODITY, False),
        ("Infosys wins order, board approves buyback", NewsCategory.CORPORATE, False),
        ("Alembic Pharma gets US FDA nod for cancer drug", NewsCategory.CORPORATE, False),
        ("Nasdaq ends higher as US futures rise", NewsCategory.GLOBAL_MARKET, False),
        ("Nifty and Sensex close flat", NewsCategory.INDIA_MARKET, False),
        ("Local festival celebrated across the city", NewsCategory.OTHER, False),
    ],
)
def test_classification_is_deterministic(
    title: str, expected: NewsCategory, long_term: bool
) -> None:
    assert classify(title, "") == (expected, long_term)


def test_keywords_match_whole_words_only() -> None:
    # "celebrated" contains "rate"; it must not be classified as RATES.
    assert classify("Local festival celebrated across the city", "")[0] is NewsCategory.OTHER
    # "warning" contains "war"; it must not be geopolitical.
    assert classify("Analyst issues warning on margins", "")[0] is not NewsCategory.GEOPOLITICAL


def test_ingest_deduplicates_by_url_and_title(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.ingest(parse_feed(FEED, ingested_at=NOW))
    assert first.fetched == 4
    assert first.stored == 4
    assert first.duplicates == 0

    # Same entries again: nothing new.
    again = store.ingest(parse_feed(FEED, ingested_at=NOW))
    assert again.stored == 0
    assert again.duplicates == 4

    # A true re-ordering of the same words is caught by the shingle hash.
    reworded = parse_feed(
        b"""<rss><channel><item>
            <title>contract large wins Wipro BP from</title>
            <link>https://other.example.com/wipro</link>
            <pubDate>Wed, 25 Nov 2026 18:00:00 +0530</pubDate>
        </item></channel></rss>""",
        ingested_at=NOW,
    )
    result = store.ingest(reworded)
    assert result.stored == 0
    assert result.duplicates == 1


def test_only_long_term_items_become_memories(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.ingest(parse_feed(FEED, ingested_at=NOW))
    # RBI (RATES) and the geopolitical item qualify; Nifty and the contract do not.
    assert result.long_term_memories == 2
    memories = store.memories()
    assert {item.category for item in memories} == {
        NewsCategory.RATES,
        NewsCategory.GEOPOLITICAL,
    }
    assert all(item.news_id for item in memories)


def test_memory_dedupes_and_respects_point_in_time_and_expiry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ingest(parse_feed(FEED, ingested_at=NOW))
    with closing(sqlite3.connect(tmp_path / "bq.db")) as connection:
        count = connection.execute("SELECT count(*) FROM agent_memories").fetchone()[0]
    assert count == 2

    # Future as_of sees nothing; a past as_of sees nothing either.
    assert store.memories(as_of=NOW - timedelta(days=1)) == ()

    later = NOW + timedelta(days=366)
    assert store.memories(as_of=later) == ()


def test_ingest_enforces_field_and_batch_bounds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    big = b"""<rss><channel><item>
        <title>%s</title>
        <description>%s</description>
        <link>https://example.com/big</link>
    </item></channel></rss>""" % (b"T" * 900, b"D" * 5000)
    entries = parse_feed(big, ingested_at=NOW)
    assert len(entries[0].title) <= 512
    assert len(entries[0].summary) <= MAX_SUMMARY_CHARS
    store.ingest(entries)
    item = store.recent(limit=1)[0]
    assert len(item.title) <= 512
    assert len(item.summary) <= MAX_SUMMARY_CHARS

    with pytest.raises(NewsError, match="between 1 and 500"):
        store.memories(limit=0)
    with pytest.raises(NewsError, match="positive"):
        store.ingest(entries, memory_ttl=timedelta(0))


def test_agent_run_uses_injected_payload_and_reports_outcome(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    agent = NewsResearchAgent(path, clock=FrozenClock())
    report = agent.run(payload=FEED)
    assert report.fetched == 4
    assert report.stored == 4
    assert report.long_term_memories == 2
    assert report.finished_at >= report.started_at
    assert len(agent.digest()) == 2


def test_agent_fetch_wrapper_never_calls_the_network_in_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)

    def explode(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("network must not be used")

    monkeypatch.setattr("bq.news_agent.fetch_feed", explode)
    agent = NewsResearchAgent(path, clock=FrozenClock())
    assert agent.run(payload=FEED).stored == 4


def test_news_digest_endpoint_is_private_and_reports_items(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    # Seed in the past: the endpoint reads with the real clock, and memories are
    # point-in-time, so a future-dated seed would (correctly) be invisible.
    past = datetime.now(UTC) - timedelta(hours=2)
    NewsStore(path, FrozenClock(past)).ingest(parse_feed(FEED, ingested_at=past))
    auth = OwnerAuth(path)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.get("/api/v1/news/digest")
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"

            auth.setup("synthetic owner password")
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": "synthetic owner password"},
            )
            assert login.status_code == 200
            csrf = login.json()["csrf_token"]

            response = await client.get("/api/v1/news/digest")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["source_id"] == "pulse.zerodha"
            assert body["item_count"] == 4
            assert body["memory_count"] == 2
            assert len(body["items"]) == 4
            assert all(item["url"].startswith("https://") for item in body["items"])

            logout = await client.post(
                "/api/v1/auth/logout",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert logout.status_code == 204
            assert (await client.get("/api/v1/news/digest")).status_code == 401

    asyncio.run(check())


def test_news_migration_preserves_existing_data(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    count = migrate(path, create=True)
    assert count >= 22
    with closing(sqlite3.connect(path)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
            )
        }
    assert {"news_items", "agent_memories"} <= tables
    assert {
        "news_items_no_update",
        "news_items_no_delete",
        "agent_memories_no_delete",
    } <= triggers


def test_stored_news_is_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ingest(parse_feed(FEED, ingested_at=NOW))
    with closing(sqlite3.connect(tmp_path / "bq.db")) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with connection, connection:
                connection.execute("UPDATE news_items SET title = 'changed'")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            with connection, connection:
                connection.execute("DELETE FROM news_items")
