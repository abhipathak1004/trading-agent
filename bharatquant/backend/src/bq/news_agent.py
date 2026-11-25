"""The news research agent.

One job: pull the news feed, store what is new, and remember what matters over
the long term — cheaply. It performs no model calls. If summarisation is ever
wanted, it belongs behind an explicit opt-in and a token budget, not here.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bq.core import Clock, SystemClock
from bq.infra.news_feed import fetch_feed
from bq.news import (
    PULSE_FEED_URL,
    PULSE_SOURCE_ID,
    AgentMemory,
    IngestResult,
    MemoryKind,
    NewsCategory,
    NewsStore,
    parse_feed,
)

AGENT_ID = "news-research"


@dataclass(frozen=True, slots=True)
class RunReport:
    source_id: str
    fetched: int
    stored: int
    duplicates: int
    long_term_memories: int
    started_at: datetime
    finished_at: datetime


class NewsResearchAgent:
    def __init__(
        self,
        path: Path,
        *,
        clock: Clock | None = None,
        feed_url: str = PULSE_FEED_URL,
        source_id: str = PULSE_SOURCE_ID,
        store: NewsStore | None = None,
    ) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.feed_url = feed_url
        self.source_id = source_id
        self.store = store or NewsStore(path, self.clock)

    def run(self, *, payload: bytes | None = None) -> RunReport:
        """One pass. ``payload`` lets a caller inject already-fetched bytes."""
        started_at = self.clock.now().astimezone(UTC)
        raw = payload if payload is not None else fetch_feed(self.feed_url)
        entries = parse_feed(raw, ingested_at=started_at)
        result: IngestResult = self.store.ingest(
            entries, source_id=self.source_id, agent_id=AGENT_ID
        )
        return RunReport(
            source_id=self.source_id,
            fetched=result.fetched,
            stored=result.stored,
            duplicates=result.duplicates,
            long_term_memories=result.long_term_memories,
            started_at=started_at,
            finished_at=self.clock.now().astimezone(UTC),
        )

    def digest(self, *, limit: int = 20) -> tuple[AgentMemory, ...]:
        """Long-term memories, newest first, for the morning report."""
        return self.store.memories(
            agent_id=AGENT_ID, kind=MemoryKind.NEWS_LONG_TERM, limit=limit
        )

    def long_term_news(self, *, limit: int = 20) -> tuple[AgentMemory, ...]:
        return self.digest(limit=limit)

    def categories(self) -> tuple[NewsCategory, ...]:
        return tuple(NewsCategory)
