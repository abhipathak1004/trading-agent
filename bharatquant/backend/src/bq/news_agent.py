"""The news research agent.

One job: pull the news feed, store what is new, and remember what matters over
the long term — cheaply. It performs no model calls. If summarisation is ever
wanted, it belongs behind an explicit opt-in and a token budget, not here.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bq.core import Clock, SystemClock
from bq.infra.news_feed import fetch_feed
from bq.news import (
    DEFAULT_MEMORY_TTL,
    PULSE_FEED_URL,
    PULSE_SOURCE_ID,
    AgentMemory,
    IngestResult,
    MemoryKind,
    NewsCategory,
    NewsStore,
    parse_feed,
)
from bq.tools.base import Tool
from bq.tools.control import ResponseTool, UnknownTool
from bq.tools.news import news_tools
from bq.tools.registry import AgentToolPolicy, ToolRegistry, ToolRunner

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

    def run(
        self, *, payload: bytes | None = None, memory_ttl_days: int | None = None
    ) -> RunReport:
        """One pass. ``payload`` lets a caller inject already-fetched bytes."""
        started_at = self.clock.now().astimezone(UTC)
        raw = payload if payload is not None else fetch_feed(self.feed_url)
        entries = parse_feed(raw, ingested_at=started_at)
        memory_ttl = (
            DEFAULT_MEMORY_TTL
            if memory_ttl_days is None
            else timedelta(days=memory_ttl_days)
        )
        result: IngestResult = self.store.ingest(
            entries,
            source_id=self.source_id,
            agent_id=AGENT_ID,
            memory_ttl=memory_ttl,
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

    def tools(self) -> tuple[Tool, ...]:
        """The toolset this agent exposes, ready to register.

        The tools wrap the same store the agent writes through, so an agent
        loop and the CLI share one source of truth. See ``bq.tools.news``.
        """
        return news_tools(self.path, feed_url=self.feed_url, clock=self.clock)

    def tool_registry(self) -> ToolRegistry:
        """A registry containing this agent's tools plus loop-control tools."""
        registry = ToolRegistry(self.tools())
        registry.register(ResponseTool())
        registry.register(UnknownTool(registry))
        return registry

    def runner(self, *, read_only: bool = False) -> ToolRunner:
        """A tool runner scoped to this agent.

        Read-only by default would hide ``news_ingest``; the research agent is
        allowed to write, so mutation is on unless a caller opts out.
        """
        return ToolRunner(
            self.tool_registry(),
            AgentToolPolicy(profile=AGENT_ID, read_only=read_only),
        )
