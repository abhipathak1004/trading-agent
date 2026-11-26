"""Fundamental / corporate-event specialist from the deterministic news store.

This is deliberately narrow until a verifiable fundamentals vendor is added: it
extracts symbol-specific corporate-action and results headlines that are already
in the point-in-time news store. It does not invent an earnings date or a
valuation, and it never fetches an article body.
"""

from __future__ import annotations

from datetime import datetime

from bq.analysis.briefs import Claim, SpecialistBrief
from bq.data.symbols import Symbol
from bq.news import NewsStore

MAX_EVENT_FACTS = 3
EVENT_KEYWORDS = (
    "earnings",
    "results",
    "quarter",
    "dividend",
    "bonus",
    "split",
    "rights",
    "buyback",
    "board meeting",
    "record date",
    "ex-date",
    "ex date",
    "merger",
    "demerger",
    "acquisition",
)


class FundamentalEventsAgent:
    def __init__(self, store: NewsStore) -> None:
        self.store = store

    def brief(self, symbol: Symbol, *, as_of: datetime) -> SpecialistBrief:
        items = self.store.recent(limit=100, as_of=as_of)
        needle = symbol.tradingsymbol.lower()
        matched = []
        for item in items:
            text = f"{item.title} {item.summary}".lower()
            if needle in text and any(word in text for word in EVENT_KEYWORDS):
                matched.append(item)
        facts: list[str] = []
        claims: list[Claim] = []
        for item in matched[:MAX_EVENT_FACTS]:
            facts.append(
                f"Corporate event {item.published_at.date().isoformat()}: {item.title}"
            )
            claims.append(
                Claim(
                    statement=f"Corporate event: {item.title}",
                    evidence_ids=(item.news_id,),
                )
            )
        if matched:
            facts.append(
                f"Event risk: {len(matched)} matching corporate headline(s) at or "
                "before as_of; verify dates and terms independently."
            )
        else:
            facts.append(
                "Event risk: no matching corporate-action or results headline at "
                "or before as_of."
            )
        return SpecialistBrief(
            agent="fundamental_events",
            symbol=symbol.canonical,
            as_of=as_of,
            facts=tuple(facts),
            claims=tuple(claims),
            source="news_store",
        )


__all__ = ["EVENT_KEYWORDS", "FundamentalEventsAgent"]
