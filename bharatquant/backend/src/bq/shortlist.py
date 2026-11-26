"""Rank the day's catalyst-bearing symbols into a 5–10 name shortlist.

Pure and deterministic: no vendor calls, no model calls, no database writes. The
campaign service supplies the catalyst rows and (optionally) a liquidity snapshot
computed from the candidate's own daily bars.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from bq.catalysts import (
    DEFAULT_EVENT_WEIGHTS,
    NEGATIVE_EVENTS,
    POSITIVE_EVENTS,
    CatalystEvent,
    StoredCatalyst,
)

MAX_SHORTLIST = 10
MIN_USEFUL_SHORTLIST = 5
NEGATIVE_PENALTY = 1.5


class ShortlistError(ValueError):
    """The shortlist request is malformed."""


@dataclass(frozen=True, slots=True)
class Liquidity:
    """What the price/volume gate needs from a symbol's recent daily bars."""

    last_close_paise: int
    avg_volume_20d: int

    def __post_init__(self) -> None:
        for name, value in (
            ("Last close", self.last_close_paise),
            ("Average volume", self.avg_volume_20d),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ShortlistError(f"{name} must be a non-negative integer.")


@dataclass(frozen=True, slots=True)
class ShortlistConfig:
    top_n: int = 8
    min_catalyst_strength: int = 3
    min_price_paise: int = 2000
    min_avg_volume: int = 100_000
    hard_negative_strength: int = 3

    def __post_init__(self) -> None:
        if isinstance(self.top_n, bool) or not 1 <= self.top_n <= MAX_SHORTLIST:
            raise ShortlistError(f"top_n must be between 1 and {MAX_SHORTLIST}.")
        if not 1 <= self.min_catalyst_strength <= 5:
            raise ShortlistError("Minimum catalyst strength must be 1..5.")
        if not 1 <= self.hard_negative_strength <= 5:
            raise ShortlistError("Hard negative strength must be 1..5.")
        if self.min_price_paise < 0 or self.min_avg_volume < 0:
            raise ShortlistError("Liquidity thresholds must not be negative.")

    def accepts(self, liquidity: Liquidity) -> bool:
        return (
            liquidity.last_close_paise >= self.min_price_paise
            and liquidity.avg_volume_20d >= self.min_avg_volume
        )


@dataclass(frozen=True, slots=True)
class ShortlistItem:
    symbol: str
    score: float
    catalysts: tuple[StoredCatalyst, ...]
    positive_strength: int
    negative_strength: int

    @property
    def events(self) -> tuple[CatalystEvent, ...]:
        return tuple(dict.fromkeys(item.event for item in self.catalysts))


@dataclass(frozen=True, slots=True)
class Shortlist:
    trade_date: date
    items: tuple[ShortlistItem, ...]
    considered: int
    unmatched_corporate: int
    insufficient: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "considered": self.considered,
            "unmatched_corporate": self.unmatched_corporate,
            "insufficient": self.insufficient,
            "items": [
                {
                    "symbol": item.symbol,
                    "score": round(item.score, 3),
                    "events": [event.value for event in item.events],
                    "positive_strength": item.positive_strength,
                    "negative_strength": item.negative_strength,
                }
                for item in self.items
            ],
        }


def _weight(
    event: CatalystEvent, weights: Mapping[CatalystEvent, float] | None
) -> float:
    if weights is not None and event in weights:
        return weights[event]
    return DEFAULT_EVENT_WEIGHTS.get(event, 0.5)


def build_shortlist(
    catalysts: Sequence[StoredCatalyst],
    *,
    trade_date: date,
    config: ShortlistConfig,
    event_weights: Mapping[CatalystEvent, float] | None = None,
    liquidity: Mapping[str, Liquidity] | None = None,
    unmatched_corporate: int = 0,
) -> Shortlist:
    """Rank symbols by positive catalyst strength and drop hard negatives."""
    if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
        raise ShortlistError("Trade date must be a calendar date.")
    if unmatched_corporate < 0:
        raise ShortlistError("Unmatched headline count must not be negative.")

    grouped: dict[str, list[StoredCatalyst]] = {}
    for item in catalysts:
        grouped.setdefault(item.symbol, []).append(item)

    ranked: list[ShortlistItem] = []
    for symbol, events in grouped.items():
        positive = [
            item
            for item in events
            if item.event in POSITIVE_EVENTS
            and item.strength >= config.min_catalyst_strength
        ]
        negative = [
            item
            for item in events
            if item.event in NEGATIVE_EVENTS
            and item.strength >= config.hard_negative_strength
        ]
        if not positive or negative:
            continue
        if liquidity is not None and symbol in liquidity:
            if not config.accepts(liquidity[symbol]):
                continue
        score = sum(
            item.strength * _weight(item.event, event_weights) for item in positive
        )
        score -= NEGATIVE_PENALTY * sum(item.strength for item in negative)
        ranked.append(
            ShortlistItem(
                symbol=symbol,
                score=score,
                catalysts=tuple(events),
                positive_strength=max(item.strength for item in positive),
                negative_strength=max((item.strength for item in negative), default=0),
            )
        )

    ranked.sort(key=lambda item: (-item.score, item.symbol))
    items = tuple(ranked[: config.top_n])
    return Shortlist(
        trade_date=trade_date,
        items=items,
        considered=len(grouped),
        unmatched_corporate=unmatched_corporate,
        insufficient=len(items) < min(MIN_USEFUL_SHORTLIST, config.top_n),
    )


__all__ = [
    "Liquidity",
    "MAX_SHORTLIST",
    "MIN_USEFUL_SHORTLIST",
    "Shortlist",
    "ShortlistConfig",
    "ShortlistError",
    "ShortlistItem",
    "build_shortlist",
]
