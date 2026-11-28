"""News context specialist: point-in-time headlines and long-term memories."""

from __future__ import annotations

from datetime import datetime

from bq.analysis.briefs import Claim, SpecialistBrief
from bq.data.symbols import Symbol
from bq.news import NewsStore

MAX_NEWS_FACTS = 5
MAX_MEMORY_FACTS = 3


class NewsContextAgent:
    """Reads the deterministic news store; makes no model calls."""

    def __init__(self, store: NewsStore) -> None:
        self.store = store

    def brief(self, symbol: Symbol, *, as_of: datetime) -> SpecialistBrief:
        items = self.store.recent(limit=100, as_of=as_of)
        memories = self.store.memories(limit=20, as_of=as_of)
        needle = symbol.tradingsymbol.lower()
        matched = [
            item for item in items if needle in f"{item.title} {item.summary}".lower()
        ]
        facts: list[str] = []
        claims: list[Claim] = []
        for item in matched[:MAX_NEWS_FACTS]:
            facts.append(
                f"News {item.published_at.date().isoformat()}: {item.title} "
                f"[{item.category.value}]"
            )
            claims.append(
                Claim(
                    statement=f"News item: {item.title}",
                    evidence_ids=(item.news_id,),
                )
            )
        for memory in memories[:MAX_MEMORY_FACTS]:
            facts.append(f"Memory ({memory.category.value}): {memory.title}")
        if not matched and not memories:
            facts.append(
                "No symbol-specific news or long-term memory at or before as_of."
            )
        elif not matched:
            facts.append("No symbol-specific headline at or before as_of.")
        return SpecialistBrief(
            agent="news",
            symbol=symbol.canonical,
            as_of=as_of,
            facts=tuple(facts),
            claims=tuple(claims),
            source="news_store",
        )


__all__ = ["NewsContextAgent"]
