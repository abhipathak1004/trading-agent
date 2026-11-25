"""Tools the news research agent can run.

These wrap the deterministic news pipeline (`bq.news`) so an agent — or the
UI, or a future model-driven loop — can query it through one uniform surface.

Every tool is read-only except ``news_ingest``, which writes. That split is
what lets the research agent hold ``news_ingest`` while a read-only analyst
profile sees only the query tools.
"""

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from bq.core import Clock, SystemClock
from bq.news import (
    DEFAULT_MEMORY_TTL,
    MAX_SUMMARY_CHARS,
    PULSE_SOURCE_ID,
    AgentMemory,
    MemoryKind,
    NewsItem,
    NewsStore,
)
from bq.tools.base import (
    Tool,
    ToolArgument,
    ToolResult,
    ToolSpec,
    bounded_text,
    decimal_int,
)

MAX_QUERY_CHARS = 200
MAX_LIMIT = 100
DEFAULT_LIMIT = 18


def _format_memories(memories: tuple[AgentMemory, ...]) -> str:
    if not memories:
        return "No long-term news memories matched."
    lines = []
    for item in memories:
        stamp = item.known_at.isoformat().replace("+00:00", "Z")
        block = f"- [{item.category.value}] {item.title}"
        if item.body:
            block += f"\n  {item.body[:MAX_SUMMARY_CHARS]}"
        block += f"\n  known_at: {stamp}"
        lines.append(block)
    return "\n".join(lines)


def _format_items(items: tuple[NewsItem, ...]) -> str:
    if not items:
        return "No recent news items matched."
    lines = []
    for item in items:
        flag = " LONG_TERM" if item.long_term_impact else ""
        stamp = item.published_at.isoformat().replace("+00:00", "Z")
        lines.append(
            f"- [{item.category.value}{flag}] {item.title}\n"
            f"  {item.url}\n  published_at: {stamp}"
        )
    return "\n".join(lines)


class NewsSearchTool(Tool):
    """Search recent ingested news deterministically."""

    spec = ToolSpec(
        name="news_search",
        summary=(
            "Search recent ingested news by an optional whole-word keyword in the "
            "title or summary. Returns newest first."
        ),
        arguments=(
            ToolArgument(
                "query",
                "Keyword to match against title and summary (case-insensitive).",
                required=False,
            ),
            ToolArgument(
                "limit",
                f"How many items to return, 1 to {MAX_LIMIT}.",
                required=False,
                kind=int,
            ),
            ToolArgument(
                "long_term_only",
                "Return only items flagged as long-term impact.",
                required=False,
                kind=bool,
            ),
        ),
        mutating=False,
    )

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def execute(self, args: Mapping[str, object]) -> ToolResult:
        query = bounded_text(
            args.get("query"), name="query", maximum=MAX_QUERY_CHARS, required=False
        )
        limit = DEFAULT_LIMIT
        if "limit" in args:
            limit = decimal_int(
                args.get("limit"), name="limit", minimum=1, maximum=MAX_LIMIT
            )
        long_term_only = args.get("long_term_only") is True
        store = NewsStore(self.path, self.clock)
        items = store.recent(limit=limit * 4, long_term_only=long_term_only)
        if query:
            needle = query.lower()
            items = tuple(
                item
                for item in items
                if needle in item.title.lower() or needle in item.summary.lower()
            )
        selected = items[:limit]
        header = (
            f"News search: query={query!r}, limit={limit}, "
            f"long_term_only={long_term_only}. {len(selected)} result(s)."
        )
        return ToolResult(message=f"{header}\n\n{_format_items(selected)}")


class NewsMemoryTool(Tool):
    """Recall the agent's long-term news memories."""

    spec = ToolSpec(
        name="news_memory",
        summary=(
            "Recall the news agent's long-term-impact memories, newest first, "
            "optionally filtered by keyword."
        ),
        arguments=(
            ToolArgument(
                "query",
                "Keyword to match against memory title and body.",
                required=False,
            ),
            ToolArgument(
                "limit",
                f"How many memories to return, 1 to {MAX_LIMIT}.",
                required=False,
                kind=int,
            ),
        ),
        mutating=False,
    )

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def execute(self, args: Mapping[str, object]) -> ToolResult:
        query = bounded_text(
            args.get("query"), name="query", maximum=MAX_QUERY_CHARS, required=False
        )
        limit = DEFAULT_LIMIT
        if "limit" in args:
            limit = decimal_int(
                args.get("limit"), name="limit", minimum=1, maximum=MAX_LIMIT
            )
        store = NewsStore(self.path, self.clock)
        memories = store.memories(kind=MemoryKind.NEWS_LONG_TERM, limit=limit * 4)
        if query:
            needle = query.lower()
            memories = tuple(
                item
                for item in memories
                if needle in item.title.lower() or needle in item.body.lower()
            )
        selected = memories[:limit]
        # Recalling a memory records the access; pruning can then use hit_count.
        if selected:
            store.note_hits([item.memory_id for item in selected])
        header = (
            f"News memory: query={query!r}, limit={limit}. "
            f"{len(selected)} memor{'y' if len(selected) == 1 else 'ies'}."
        )
        return ToolResult(message=f"{header}\n\n{_format_memories(selected)}")


class NewsDigestTool(Tool):
    """A compact morning-digest style summary of recent and long-term news."""

    spec = ToolSpec(
        name="news_digest",
        summary=(
            "Produce a compact digest: counts plus the newest long-term memories "
            "and recent headlines, for a briefing."
        ),
        arguments=(
            ToolArgument(
                "limit",
                f"How many memories and items to include, 1 to {MAX_LIMIT}.",
                required=False,
                kind=int,
            ),
        ),
        mutating=False,
    )

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def execute(self, args: Mapping[str, object]) -> ToolResult:
        limit = DEFAULT_LIMIT
        if "limit" in args:
            limit = decimal_int(
                args.get("limit"), name="limit", minimum=1, maximum=MAX_LIMIT
            )
        store = NewsStore(self.path, self.clock)
        items = store.recent(limit=limit)
        memories = store.memories(kind=MemoryKind.NEWS_LONG_TERM, limit=limit)
        message = (
            f"News digest: {len(items)} recent item(s), "
            f"{len(memories)} long-term memor{'y' if len(memories) == 1 else 'ies'}.\n\n"
            f"## Long-term memory\n{_format_memories(memories)}\n\n"
            f"## Recent headlines\n{_format_items(items)}"
        )
        return ToolResult(message=message)


class NewsIngestTool(Tool):
    """Fetch the configured feed, deduplicate, classify and store."""

    spec = ToolSpec(
        name="news_ingest",
        summary=(
            "Fetch the configured news feed, store new items and promote "
            "long-term-impact stories to memory. Deterministic; no model calls."
        ),
        arguments=(
            ToolArgument(
                "memory_ttl_days",
                "Override memory retention in days for this run.",
                required=False,
                kind=int,
            ),
        ),
        mutating=True,
    )

    def __init__(
        self, path: Path, *, feed_url: str, clock: Clock | None = None
    ) -> None:
        self.path = path
        self.feed_url = feed_url
        self.clock = clock or SystemClock()

    def execute(self, args: Mapping[str, object]) -> ToolResult:
        memory_ttl_days: int | None = None
        if "memory_ttl_days" in args:
            memory_ttl_days = decimal_int(
                args.get("memory_ttl_days"),
                name="memory_ttl_days",
                minimum=1,
                maximum=DEFAULT_MEMORY_TTL.days * 10,
            )
        from bq.news_agent import NewsResearchAgent

        agent = NewsResearchAgent(
            self.path,
            clock=self.clock,
            feed_url=self.feed_url,
            source_id=PULSE_SOURCE_ID,
        )
        report = agent.run(memory_ttl_days=memory_ttl_days)
        elapsed = timedelta(0)
        if report.finished_at >= report.started_at:
            elapsed = report.finished_at - report.started_at
        return ToolResult(
            message=(
                f"News ingest complete: fetched {report.fetched}, stored "
                f"{report.stored}, duplicates {report.duplicates}, new long-term "
                f"memories {report.long_term_memories}. "
                f"Elapsed {elapsed.total_seconds():.2f}s from {report.source_id}."
            )
        )


def news_tools(
    path: Path,
    *,
    feed_url: str,
    clock: Clock | None = None,
) -> tuple[Tool, ...]:
    """The full news toolset for one database."""
    return (
        NewsSearchTool(path, clock),
        NewsMemoryTool(path, clock),
        NewsDigestTool(path, clock),
        NewsIngestTool(path, feed_url=feed_url, clock=clock),
    )
