"""News research agent: ingest, deduplicate and remember market news.

Cost design
-----------
The feed already carries a one-line summary per story, so **full articles are
never fetched**. Deduplication and classification are deterministic: hashes and
keyword rules, no model calls. Expensive summarisation is deliberately absent
from this module; long-term memory stores the compact record only.

Every write is bounded (feed bytes, item count, field lengths) so a misbehaving
or hostile feed cannot inflate storage or latency.
"""

import re
import secrets
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree

from bq.core import Clock, SystemClock
from bq.infra.audit import append_event

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

PULSE_SOURCE_ID = "pulse.zerodha"
PULSE_FEED_URL = "https://pulse.zerodha.com/feed.xml"

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ITEMS_PER_INGEST = 500
MAX_TITLE_CHARS = 512
MAX_SUMMARY_CHARS = 1024
MAX_URL_CHARS = 2048
DEFAULT_MEMORY_TTL = timedelta(days=365)

_WORD = re.compile(r"[a-z0-9]+")


class NewsError(ValueError):
    pass


class NewsCategory(StrEnum):
    INDIA_MARKET = "INDIA_MARKET"
    GLOBAL_MARKET = "GLOBAL_MARKET"
    GEOPOLITICAL = "GEOPOLITICAL"
    COMMODITY = "COMMODITY"
    RATES = "RATES"
    CORPORATE = "CORPORATE"
    REGULATORY = "REGULATORY"
    OTHER = "OTHER"


class MemoryKind(StrEnum):
    NEWS_LONG_TERM = "NEWS_LONG_TERM"
    NEWS_DEDUPE = "NEWS_DEDUPE"
    REFLECTION = "REFLECTION"


# Ordered: the first matching rule wins, so the most consequential categories
# are checked before the generic ones. Keywords are matched on word boundaries,
# so "celebrated" never matches "rate".
_CATEGORY_RULES: tuple[tuple[NewsCategory, tuple[str, ...]], ...] = (
    (
        NewsCategory.GEOPOLITICAL,
        (
            "war",
            "wars",
            "sanction",
            "sanctions",
            "tariff",
            "tariffs",
            "geopolitical",
            "geopolitics",
            "border",
            "treaty",
            "election",
            "elections",
            "coup",
            "conflict",
            "ceasefire",
            "nato",
            "opec",
            "embargo",
            "missile",
            "missiles",
            "invasion",
            "diplomatic",
        ),
    ),
    (
        NewsCategory.RATES,
        (
            "rbi",
            "repo",
            "interest",
            "monetary",
            "inflation",
            "cpi",
            "wpi",
            "fed",
            "federal",
            "treasury",
            "yield",
            "yields",
            "bond",
            "bonds",
            "rate",
            "rates",
            "policy",
        ),
    ),
    (
        NewsCategory.COMMODITY,
        (
            "crude",
            "brent",
            "wti",
            "gold",
            "silver",
            "copper",
            "gas",
            "commodity",
            "commodities",
            "bullion",
            "steel",
            "coal",
            "zinc",
            "aluminium",
            "aluminum",
        ),
    ),
    (
        NewsCategory.REGULATORY,
        (
            "sebi",
            "irdai",
            "trai",
            "cci",
            "regulation",
            "regulations",
            "regulator",
            "regulators",
            "penalty",
            "penalties",
            "probe",
            "investigation",
            "circular",
            "compliance",
            "insider",
            "fraud",
        ),
    ),
    (
        NewsCategory.CORPORATE,
        (
            "results",
            "earnings",
            "profit",
            "revenue",
            "merger",
            "mergers",
            "acquisition",
            "acquisitions",
            "acquires",
            "ipo",
            "qip",
            "buyback",
            "dividend",
            "bonus",
            "rights",
            "stake",
            "board",
            "ceo",
            "appointment",
            "resignation",
            "demerger",
            "order",
            "orders",
            "contract",
            "contracts",
            "fda",
            "nod",
            "approval",
            "approves",
            "cleared",
            "patent",
            "partners",
            "partnership",
            "joint",
            "venture",
            "stake",
            "stake sale",
            "q2",
            "q3",
            "q4",
            "h1",
            "h2",
            "profit",
            "ebitda",
            "margin",
            "guidance",
        ),
    ),
    (
        NewsCategory.GLOBAL_MARKET,
        (
            "nasdaq",
            "dow",
            "wall",
            "ftse",
            "nikkei",
            "hang",
            "global",
            "asian",
            "european",
            "futures",
            "s&p",
        ),
    ),
    (
        NewsCategory.INDIA_MARKET,
        (
            "nifty",
            "sensex",
            "bse",
            "nse",
            "fii",
            "dii",
            "midcap",
            "smallcap",
            "rupee",
            "dalal",
            "equity",
            "equities",
            "stock",
            "stocks",
            "mutual",
            "sip",
            "market",
            "markets",
            "index",
        ),
    ),
)

_CATEGORY_PATTERNS: tuple[tuple[NewsCategory, re.Pattern[str]], ...] = tuple(
    (
        category,
        re.compile(
            r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, keywords)) + r")(?![a-z0-9])"
        ),
    )
    for category, keywords in _CATEGORY_RULES
)

# Words that mark an item as likely to matter beyond the current session.
# Deliberately narrow: everyday business news ("wins contract", "order win") is
# not structural, and remembering it would drown the signal.
_LONG_TERM_MARKERS: tuple[str, ...] = (
    "policy",
    "rate",
    "rates",
    "sanction",
    "sanctions",
    "tariff",
    "tariffs",
    "war",
    "treaty",
    "reform",
    "reforms",
    "budget",
    "stimulus",
    "recession",
    "default",
    "downgrade",
    "downgrades",
    "upgrade",
    "upgrades",
    "regulation",
    "ban",
    "bans",
    "investigation",
    "fraud",
    "merger",
    "mergers",
    "acquisition",
    "acquisitions",
    "structural",
    "bailout",
    "moratorium",
)

_LONG_TERM_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(map(re.escape, _LONG_TERM_MARKERS))
    + r")(?![a-z0-9])"
)

# Categories whose events are structural by definition: a geopolitical or
# regulatory event changes the backdrop, not just the session.
# RATES is excluded: most rate headline flow is routine (daily RBI notices,
# gold reserves), so it must earn long-term status from the markers below.
_LONG_TERM_CATEGORIES = frozenset(
    {
        NewsCategory.GEOPOLITICAL,
        NewsCategory.REGULATORY,
    }
)

# Rate/policy headline words that turn a routine RATES item into a structural one.
_STRUCTURAL_RATE_MARKERS: tuple[str, ...] = (
    "repo",
    "policy rate",
    "monetary policy",
    "rate cut",
    "rate hike",
    "hikes",
    "cuts",
    "inflation",
    "cpi",
    "stimulus",
    "recession",
    "quantitative",
)

_STRUCTURAL_RATE_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(map(re.escape, _STRUCTURAL_RATE_MARKERS))
    + r")(?![a-z0-9])"
)


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """One parsed feed item, before dedupe and storage."""

    title: str
    summary: str
    url: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class NewsItem:
    news_id: str
    source_id: str
    url: str
    title: str
    summary: str
    category: NewsCategory
    long_term_impact: bool
    published_at: datetime
    ingested_at: datetime


@dataclass(frozen=True, slots=True)
class AgentMemory:
    memory_id: str
    agent_id: str
    kind: MemoryKind
    news_id: str | None
    category: NewsCategory
    title: str
    body: str
    known_at: datetime
    expires_at: datetime | None
    hit_count: int


@dataclass(frozen=True, slots=True)
class IngestResult:
    fetched: int
    stored: int
    duplicates: int
    long_term_memories: int


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise NewsError("News timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _collapse(text: str) -> str:
    return " ".join(text.split())


def normalized_url(url: str) -> str:
    """Lower-case scheme and host, drop the fragment, keep the path and query."""
    value = url.strip()
    if not value:
        raise NewsError("News URL is empty.")
    scheme, separator, remainder = value.partition("://")
    if not separator or scheme.lower() != "https":
        raise NewsError("News URLs must use HTTPS.")
    host, slash, path = remainder.partition("/")
    if not host:
        raise NewsError("News URL has no host.")
    return f"https://{host.lower()}{slash}{path.split('#', 1)[0]}"


def url_hash(url: str) -> str:
    return _sha256(normalized_url(url))


def _title_tokens(title: str) -> list[str]:
    return _WORD.findall(title.lower())


def shingle_hash(title: str) -> str:
    """Order-insensitive fingerprint of the title, for near-duplicate detection."""
    tokens = _title_tokens(title)
    if not tokens:
        raise NewsError("News title has no usable words.")
    return _sha256(" ".join(sorted(tokens)))


def classify(title: str, summary: str) -> tuple[NewsCategory, bool]:
    """Return the deterministic category and long-term-impact hint."""
    haystack = f"{title} {summary}".lower()
    category = NewsCategory.OTHER
    for candidate, pattern in _CATEGORY_PATTERNS:
        if pattern.search(haystack):
            category = candidate
            break
    if category in _LONG_TERM_CATEGORIES:
        return category, True
    if category is NewsCategory.RATES:
        # Routine rate flow stays short-term; structural policy moves persist.
        return category, bool(_STRUCTURAL_RATE_PATTERN.search(haystack))
    return category, bool(_LONG_TERM_PATTERN.search(haystack))


def _text(element: ElementTree.Element, tag: str) -> str:
    child = element.find(tag)
    if child is None or child.text is None:
        return ""
    return _collapse(child.text)


def parse_feed(
    payload: bytes,
    *,
    ingested_at: datetime,
    max_items: int = MAX_ITEMS_PER_INGEST,
) -> tuple[FeedEntry, ...]:
    """Parse an RSS feed into bounded entries. Never fetches an article."""
    if not isinstance(payload, bytes):
        raise NewsError("Feed payload must be bytes.")
    if len(payload) > MAX_FEED_BYTES:
        raise NewsError("Feed payload exceeds the size limit.")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise NewsError("Feed item limit must be a positive integer.")
    _utc(ingested_at)
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise NewsError("Feed payload is not valid XML.") from exc

    entries: list[FeedEntry] = []
    for item in root.iter("item"):
        if len(entries) >= max_items:
            break
        title = _text(item, "title")[:MAX_TITLE_CHARS]
        link = _text(item, "link")
        if not title or not link:
            continue
        summary = _text(item, "description")[:MAX_SUMMARY_CHARS]
        published_text = _text(item, "pubDate")
        try:
            published = (
                parsedate_to_datetime(published_text) if published_text else ingested_at
            )
        except (TypeError, ValueError):
            published = ingested_at
        if published.utcoffset() is None:
            published = published.replace(tzinfo=UTC)
        if _utc(published) > _utc(ingested_at):
            # A feed claiming a future publication time is not trustworthy.
            published = ingested_at
        entries.append(
            FeedEntry(
                title=title,
                summary=summary,
                url=normalized_url(link),
                published_at=_utc(published),
            )
        )
    return tuple(entries)


class NewsStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def ingest(
        self,
        entries: Iterable[FeedEntry],
        *,
        source_id: str = PULSE_SOURCE_ID,
        agent_id: str = "news-research",
        memory_ttl: timedelta = DEFAULT_MEMORY_TTL,
    ) -> IngestResult:
        if IDENTIFIER.fullmatch(source_id) is None:
            raise NewsError("Invalid news source identifier.")
        if IDENTIFIER.fullmatch(agent_id) is None:
            raise NewsError("Invalid agent identifier.")
        if not isinstance(memory_ttl, timedelta) or memory_ttl <= timedelta(0):
            raise NewsError("Memory TTL must be a positive timedelta.")

        ingested_at = _utc(self.clock.now())
        batch = tuple(entries)
        if len(batch) > MAX_ITEMS_PER_INGEST:
            raise NewsError("Too many feed entries in one ingest.")

        stored = 0
        duplicates = 0
        long_term = 0
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                for entry in batch:
                    url_digest = url_hash(entry.url)
                    shingle = shingle_hash(entry.title)
                    existing = connection.execute(
                        "SELECT 1 FROM news_items WHERE url_hash = ? OR shingle_hash = ?",
                        (url_digest, shingle),
                    ).fetchone()
                    if existing is not None:
                        duplicates += 1
                        continue

                    news_id = secrets.token_urlsafe(18)
                    category, impact = classify(entry.title, entry.summary)
                    item_hash = _sha256(
                        "\n".join(
                            (
                                url_digest,
                                shingle,
                                category.value,
                                "1" if impact else "0",
                                entry.title,
                                entry.summary,
                            )
                        )
                    )
                    connection.execute(
                        "INSERT INTO news_items "
                        "(news_id, source_id, url, url_hash, title, summary, "
                        "shingle_hash, category, long_term_impact, published_at, "
                        "ingested_at, item_hash) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            news_id,
                            source_id,
                            entry.url[:MAX_URL_CHARS],
                            url_digest,
                            entry.title,
                            entry.summary,
                            shingle,
                            category.value,
                            1 if impact else 0,
                            _timestamp(entry.published_at),
                            _timestamp(ingested_at),
                            item_hash,
                        ),
                    )
                    stored += 1
                    if impact:
                        self._remember_news(
                            connection,
                            agent_id=agent_id,
                            news_id=news_id,
                            category=category,
                            title=entry.title,
                            body=entry.summary,
                            known_at=ingested_at,
                            memory_ttl=memory_ttl,
                        )
                        long_term += 1

                if stored:
                    append_event(
                        connection,
                        occurred_at=ingested_at,
                        actor="owner",
                        action="research.news.ingested",
                        object_type="news_batch",
                        object_id=source_id,
                        payload={
                            "stored": str(stored),
                            "duplicates": str(duplicates),
                            "long_term": str(long_term),
                        },
                    )
        return IngestResult(
            fetched=len(batch),
            stored=stored,
            duplicates=duplicates,
            long_term_memories=long_term,
        )

    def _remember_news(
        self,
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        news_id: str,
        category: NewsCategory,
        title: str,
        body: str,
        known_at: datetime,
        memory_ttl: timedelta,
    ) -> None:
        content_hash = _sha256(f"{category.value}\n{title}")
        existing = connection.execute(
            "SELECT 1 FROM agent_memories "
            "WHERE agent_id = ? AND kind = ? AND content_hash = ?",
            (agent_id, MemoryKind.NEWS_LONG_TERM.value, content_hash),
        ).fetchone()
        if existing is not None:
            return
        connection.execute(
            "INSERT INTO agent_memories "
            "(memory_id, agent_id, kind, news_id, category, title, body, "
            "content_hash, known_at, expires_at, hit_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                secrets.token_urlsafe(18),
                agent_id,
                MemoryKind.NEWS_LONG_TERM.value,
                news_id,
                category.value,
                title[:MAX_TITLE_CHARS],
                body[:MAX_SUMMARY_CHARS],
                content_hash,
                _timestamp(known_at),
                _timestamp(known_at + memory_ttl),
                _timestamp(known_at),
            ),
        )

    def recent(
        self,
        *,
        limit: int = 50,
        long_term_only: bool = False,
    ) -> tuple[NewsItem, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise NewsError("News limit must be between 1 and 500.")
        query = (
            "SELECT news_id, source_id, url, title, summary, category, "
            "long_term_impact, published_at, ingested_at FROM news_items "
        )
        params: tuple[object, ...]
        if long_term_only:
            query += "WHERE long_term_impact = 1 "
            params = ()
        else:
            params = ()
        query += "ORDER BY published_at DESC, news_id LIMIT ?"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return tuple(
            NewsItem(
                news_id=str(row[0]),
                source_id=str(row[1]),
                url=str(row[2]),
                title=str(row[3]),
                summary=str(row[4]),
                category=NewsCategory(str(row[5])),
                long_term_impact=bool(row[6]),
                published_at=_parse_timestamp(str(row[7])),
                ingested_at=_parse_timestamp(str(row[8])),
            )
            for row in rows
        )

    def memories(
        self,
        *,
        agent_id: str = "news-research",
        kind: MemoryKind = MemoryKind.NEWS_LONG_TERM,
        limit: int = 50,
        as_of: datetime | None = None,
    ) -> tuple[AgentMemory, ...]:
        """Return active memories, point-in-time when ``as_of`` is supplied."""
        if IDENTIFIER.fullmatch(agent_id) is None:
            raise NewsError("Invalid agent identifier.")
        if not isinstance(kind, MemoryKind):
            raise NewsError("Invalid memory kind.")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise NewsError("Memory limit must be between 1 and 500.")
        pivot = _timestamp(as_of) if as_of is not None else _timestamp(self.clock.now())
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT memory_id, agent_id, kind, news_id, category, title, body, "
                "known_at, expires_at, hit_count FROM agent_memories "
                "WHERE agent_id = ? AND kind = ? AND known_at <= ? "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY known_at DESC, memory_id LIMIT ?",
                (agent_id, kind.value, pivot, pivot, limit),
            ).fetchall()
        return tuple(
            AgentMemory(
                memory_id=str(row[0]),
                agent_id=str(row[1]),
                kind=MemoryKind(str(row[2])),
                news_id=str(row[3]) if row[3] is not None else None,
                category=NewsCategory(str(row[4])),
                title=str(row[5]),
                body=str(row[6]),
                known_at=_parse_timestamp(str(row[7])),
                expires_at=_parse_timestamp(str(row[8]))
                if row[8] is not None
                else None,
                hit_count=int(row[9]),
            )
            for row in rows
        )

    def note_hits(self, memory_ids: Iterable[str]) -> None:
        """Bump retrieval counts; used later for frecency-based pruning."""
        ids = [value for value in memory_ids if IDENTIFIER.fullmatch(value)]
        if not ids:
            return
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.executemany(
                    "UPDATE agent_memories SET hit_count = hit_count + 1 "
                    "WHERE memory_id = ?",
                    [(value,) for value in ids],
                )
