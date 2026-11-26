"""Typed specialist briefs shared by the Stage 1 research agents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MAX_FACTS = 10
MAX_FACT_CHARS = 320
MAX_CLAIM_CHARS = 400
MAX_EVIDENCE_IDS = 8

SPECIALIST_ORDER: tuple[str, ...] = (
    "news",
    "market_regime",
    "fundamental_events",
)


class BriefError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Claim:
    statement: str
    evidence_ids: tuple[str, ...] = ()
    verified: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.statement, str) or not 1 <= len(self.statement) <= (
            MAX_CLAIM_CHARS
        ):
            raise BriefError("A claim must be bounded text.")
        if not isinstance(self.evidence_ids, tuple) or len(self.evidence_ids) > (
            MAX_EVIDENCE_IDS
        ):
            raise BriefError("A claim has too many evidence identifiers.")
        if not all(isinstance(item, str) and item for item in self.evidence_ids):
            raise BriefError("Claim evidence identifiers must be text.")

    def to_dict(self) -> dict[str, object]:
        return {
            "statement": self.statement,
            "evidence_ids": list(self.evidence_ids),
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class SpecialistBrief:
    agent: str
    symbol: str
    as_of: datetime
    facts: tuple[str, ...]
    claims: tuple[Claim, ...] = ()
    degraded: bool = False
    source: str = ""

    def __post_init__(self) -> None:
        if not self.agent or not self.symbol:
            raise BriefError("A brief needs an agent and a symbol.")
        if self.as_of.utcoffset() is None:
            raise BriefError("A brief's as_of must be timezone-aware.")
        if not isinstance(self.facts, tuple) or len(self.facts) > MAX_FACTS:
            raise BriefError("A brief has too many facts.")
        if not all(
            isinstance(fact, str) and 1 <= len(fact) <= MAX_FACT_CHARS
            for fact in self.facts
        ):
            raise BriefError("Brief facts must be bounded text.")

    def to_dict(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "facts": list(self.facts),
            "claims": [claim.to_dict() for claim in self.claims],
            "degraded": self.degraded,
            "source": self.source,
        }


__all__ = [
    "BriefError",
    "Claim",
    "MAX_FACT_CHARS",
    "MAX_FACTS",
    "SPECIALIST_ORDER",
    "SpecialistBrief",
]
