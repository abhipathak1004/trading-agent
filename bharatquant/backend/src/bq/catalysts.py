"""Turn a news headline into a symbol-attributed, polarised catalyst.

The feed classifier in :mod:`bq.news` only assigns a topical bucket: a ₹2,000
crore order win and a CEO resignation both become ``CORPORATE``. This module is
the missing deterministic second pass. It answers three questions per headline:

* **Which company?** Company-name and ticker aliases from the NSE listing, so
  "Sun Pharmaceutical" reaches ``SUNPHARMA``.
* **What happened?** A closed event taxonomy (order win/loss, strong/weak
  results, guidance, capacity, buyback, regulatory action, management exit,
  dilution).
* **How strong, in which direction?** A bounded integer strength and a polarity
  of -1/0/+1, with rupee amounts parsed into a magnitude bucket.

No model runs here. Every decision is a regex or a dictionary lookup, and every
write is bounded, so a noisy feed cannot turn into unbounded spend or storage.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from bq.core import (
    EXCHANGE_TIMEZONE,
    Clock,
    SystemClock,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    utc as core_utc,
)
from bq.data.equity_master import EquityAlias
from bq.infra.audit import append_event
from bq.news import NewsCategory, NewsItem

MAX_SYMBOLS_PER_ITEM = 3
MAX_EVENTS_PER_ITEM = 5
MAX_MATCHED_TERM_CHARS = 80
MAX_SCAN_ITEMS = 500
MAX_ALIAS_WORDS = 4

# Tickers that are also ordinary words in market headlines. They are never
# treated as a company mention on their own.
GENERIC_NAME_KEYS = frozenset(
    {
        "auto",
        "bank",
        "capital",
        "cement",
        "energy",
        "finance",
        "first",
        "global",
        "health",
        "india",
        "industries",
        "infra",
        "infrastructure",
        "media",
        "motors",
        "pharma",
        "power",
        "products",
        "retail",
        "services",
        "solutions",
        "standard",
        "steel",
        "systems",
        "take",
        "technologies",
        "wealth",
    }
)

GENERIC_TICKERS = frozenset(
    {
        "ALL",
        "ANY",
        "BANK",
        "BANKS",
        "BOARD",
        "BUY",
        "CEO",
        "CFO",
        "CRUDE",
        "GOLD",
        "GST",
        "INDIA",
        "INDIAN",
        "IPO",
        "MARKET",
        "MARKETS",
        "NEWS",
        "NIFTY",
        "NSE",
        "BSE",
        "ORDER",
        "ORDERS",
        "PROFIT",
        "QIP",
        "RBI",
        "RESULTS",
        "REVENUE",
        "RUPEE",
        "SEBI",
        "SELL",
        "SENSEX",
        "SHARE",
        "SHARES",
        "STOCK",
        "STOCKS",
    }
)

_MONEY_EVENTS = frozenset(
    {
        "ORDER_WIN",
        "ORDER_LOSS",
        "ACQUISITION",
        "CAPACITY_EXPANSION",
    }
)


class CatalystEvent(StrEnum):
    ORDER_WIN = "ORDER_WIN"
    EARNINGS_STRONG = "EARNINGS_STRONG"
    GUIDANCE_RAISE = "GUIDANCE_RAISE"
    CAPACITY_EXPANSION = "CAPACITY_EXPANSION"
    REGULATORY_APPROVAL = "REGULATORY_APPROVAL"
    BUYBACK = "BUYBACK"
    DIVIDEND = "DIVIDEND"
    ACQUISITION = "ACQUISITION"
    ORDER_LOSS = "ORDER_LOSS"
    EARNINGS_WEAK = "EARNINGS_WEAK"
    GUIDANCE_CUT = "GUIDANCE_CUT"
    REGULATORY_ACTION = "REGULATORY_ACTION"
    MANAGEMENT_EXIT = "MANAGEMENT_EXIT"
    DILUTION = "DILUTION"
    CORPORATE_OTHER = "CORPORATE_OTHER"


POSITIVE_EVENTS = frozenset(
    {
        CatalystEvent.ORDER_WIN,
        CatalystEvent.EARNINGS_STRONG,
        CatalystEvent.GUIDANCE_RAISE,
        CatalystEvent.CAPACITY_EXPANSION,
        CatalystEvent.REGULATORY_APPROVAL,
        CatalystEvent.BUYBACK,
        CatalystEvent.DIVIDEND,
        CatalystEvent.ACQUISITION,
    }
)

NEGATIVE_EVENTS = frozenset(
    {
        CatalystEvent.ORDER_LOSS,
        CatalystEvent.EARNINGS_WEAK,
        CatalystEvent.GUIDANCE_CUT,
        CatalystEvent.REGULATORY_ACTION,
        CatalystEvent.MANAGEMENT_EXIT,
        CatalystEvent.DILUTION,
    }
)

POLARITY = {
    **{event: 1 for event in POSITIVE_EVENTS},
    **{event: -1 for event in NEGATIVE_EVENTS},
    CatalystEvent.CORPORATE_OTHER: 0,
}

# Default score multipliers, before the learned feedback weights are applied.
DEFAULT_EVENT_WEIGHTS = {
    CatalystEvent.ORDER_WIN: 1.0,
    CatalystEvent.EARNINGS_STRONG: 1.0,
    CatalystEvent.GUIDANCE_RAISE: 0.9,
    CatalystEvent.CAPACITY_EXPANSION: 0.7,
    CatalystEvent.REGULATORY_APPROVAL: 0.9,
    CatalystEvent.BUYBACK: 0.7,
    CatalystEvent.DIVIDEND: 0.5,
    CatalystEvent.ACQUISITION: 0.6,
    CatalystEvent.CORPORATE_OTHER: 0.1,
}

MAX_STRENGTH = 5
MIN_STRENGTH = 1

_MONEY = re.compile(
    r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(crore|cr|lakh|thousand)",
    re.IGNORECASE,
)
_AMOUNT_WITHOUT_SYMBOL = re.compile(
    r"\b([0-9][0-9,]*(?:\.[0-9]+)?)[- ]?(crore|cr|lakh)\b", re.IGNORECASE
)
_WHITESPACE = re.compile(r"\s+")
_ROUNDUP = re.compile(
    r"\b(?:buy,?\s*sell\s*or\s*hold|stocks?\s+in\s+focus|"
    r"stocks?\s+to\s+watch|top\s+\d+|among\s+\d+\s+stocks?|"
    r"q[1-4]\s+results?\s+today|market\s+live|trade\s+setup|"
    r"highest\s+(?:fii|dii)|stocks?\s+with\s+highest)\b",
    re.I,
)
_PUBLISHER = re.compile(r"\s+(?:-|\||\u2014)\s+[^-|\u2014]{2,80}$")
_MANUAL_SYMBOLS = (
    (re.compile(r"\bl\s*&\s*t\b|\blarsen\s*(?:&|and)\s*toubro\b", re.I), "LT"),
    (re.compile(r"\bsbi\b", re.I), "SBIN"),
    (re.compile(r"\bhul\b", re.I), "HINDUNILVR"),
    (re.compile(r"\bril\b", re.I), "RELIANCE"),
)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_NAME_WORD = re.compile(r"[a-z0-9]+")


class CatalystError(ValueError):
    """A catalyst row is malformed or the date range is invalid."""


@dataclass(frozen=True, slots=True)
class _Rule:
    event: CatalystEvent
    pattern: re.Pattern[str]
    base: int


# Ordered: loss/weak rules come before win/strong rules so "cancels order" is
# never read as an order win. Every rule is anchored on word boundaries.
_RULES: tuple[_Rule, ...] = (
    _Rule(
        CatalystEvent.ORDER_LOSS,
        re.compile(
            r"\b(?:orders?|contracts?|deals?)\b.{0,40}"
            r"\b(?:cancel\w*|terminat\w*|call(?:ed)? off|scrapped?|lost|void)\b"
            r"|\b(?:loses?|lost|scraps?|cancels?)\b.{0,30}"
            r"\b(?:order|contract|deal)\b"
        ),
        3,
    ),
    _Rule(
        CatalystEvent.EARNINGS_WEAK,
        re.compile(
            r"\b(?:profit|net profit|pat|revenue|sales|earnings|ebitda|margin)\b"
            r".{0,40}\b(?:falls?|fell|drops?|declines?|declined|slips?|down|"
            r"miss(?:es|ed)?|below|weak|lower)\b"
            r"|\b(?:net )?loss\b.{0,30}\b(?:widens?|rises?|increases?|grows?)\b"
        ),
        3,
    ),
    _Rule(
        CatalystEvent.GUIDANCE_CUT,
        re.compile(
            r"\b(?:guidance|outlook|forecast)\b.{0,30}"
            r"\b(?:cut|lower\w*|reduc\w*|down|withdrawn?)\b"
        ),
        3,
    ),
    _Rule(
        CatalystEvent.REGULATORY_APPROVAL,
        re.compile(
            r"\b(?:fda|usfda|dcgi|cdsco|ema)\b.{0,40}"
            r"\b(?:nod|approv\w*|clear\w*|authoris\w*|authoriz\w*)\b"
            r"|\b(?:gets?|receives?|wins?)\b.{0,30}"
            r"\b(?:fda|usfda|dcgi|cdsco)\b.{0,20}"
            r"\b(?:nod|approv\w*|clear\w*)\b"
            r"|\bpatent\b"
        ),
        3,
    ),
    _Rule(
        CatalystEvent.REGULATORY_ACTION,
        re.compile(
            r"\b(?:sebi|irdai|cci|trai|penalt(?:y|ies)|fined?|fines?|probe|"
            r"investigation|banned?|show cause|raid(?:ed)?|fraud|"
            r"insider trading|notice)\b"
        ),
        3,
    ),
    _Rule(
        CatalystEvent.MANAGEMENT_EXIT,
        re.compile(
            r"\b(?:ceo|cfo|coo|managing director|md|chairman|chairperson)\b"
            r".{0,30}\b(?:resign\w*|quits?|steps? down|exits?|retires?)\b"
        ),
        2,
    ),
    _Rule(
        CatalystEvent.DILUTION,
        re.compile(
            r"\b(?:qip|qualified institutional placement|preferential "
            r"(?:issue|allotment)|rights issue|dilution|fund ?rais(?:e|ing)|"
            r"fundrais(?:e|ing))\b"
        ),
        1,
    ),
    _Rule(
        CatalystEvent.ORDER_WIN,
        re.compile(
            r"\b(?:wins?|won|bags?|bagged|secures?|secured|receives?|received|"
            r"awarded|gets?|got)\b.{0,60}"
            r"\b(?:order|orders|contract|contracts|deal|project|tender)\b"
            r"|\b(?:order|contract|deal)\b.{0,40}"
            r"\b(?:worth|valued|rs\.?|inr|₹)\b"
            r"|\b(?:new|repeat|bulk|large)\s+orders?\b"
            r"|\b(?:order|contract)\s+(?:win|wins|bagged|secured|received)\b"
            r"|\b(?:work|purchase)\s+orders?\b"
        ),
        3,
    ),
    _Rule(
        CatalystEvent.EARNINGS_STRONG,
        re.compile(
            r"\b(?:profit|net profit|pat|revenue|sales|earnings|ebitda|margin)\b"
            r".{0,40}\b(?:rises?|rose|jumps?|surges?|grows?|up|beats?|ahead|"
            r"record|strong|higher|expands?)\b"
            r"|\b(?:beats?|tops?)\b.{0,30}\b(?:estimates?|expectations?|consensus)\b"
            r"|\b(?:record|strong)\s+(?:profit|revenue|results)\b"
        ),
        3,
    ),
    _Rule(
        CatalystEvent.GUIDANCE_RAISE,
        re.compile(
            r"\b(?:guidance|outlook|forecast)\b.{0,30}"
            r"\b(?:rais\w*|hike\w*|upgrad\w*|increas\w*|higher)\b"
        ),
        3,
    ),
    _Rule(
        CatalystEvent.CAPACITY_EXPANSION,
        re.compile(
            r"\b(?:commissions?|commissioned|inaugurat\w*|capacity expansion|"
            r"expansion|new plant|greenfield|brownfield|invests?|investment)\b"
        ),
        2,
    ),
    _Rule(
        CatalystEvent.BUYBACK,
        re.compile(r"\bbuy[- ]?back\b|\brepurchase\b"),
        2,
    ),
    _Rule(
        CatalystEvent.DIVIDEND,
        re.compile(r"\bdividend\b"),
        1,
    ),
    _Rule(
        CatalystEvent.ACQUISITION,
        re.compile(
            r"\b(?:acquir\w*|acquisition|merger|merges?|stake (?:buy|purchase)|"
            r"to buy)\b"
        ),
        2,
    ),
)


@dataclass(frozen=True, slots=True)
class Catalyst:
    news_id: str
    symbol: str
    event: CatalystEvent
    polarity: int
    strength: int
    matched_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoredCatalyst:
    catalyst_id: str
    news_id: str
    symbol: str
    event: CatalystEvent
    polarity: int
    strength: int
    matched_terms: tuple[str, ...]
    trade_date: date
    extracted_at: datetime
    title: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class AliasIndex:
    """Deterministic lookup tables built once per extraction pass."""

    name_keys: Mapping[str, str]
    tickers: Mapping[str, str]

    @classmethod
    def from_aliases(cls, aliases: Iterable[EquityAlias]) -> AliasIndex:
        name_seen: dict[str, str | None] = {}
        tickers: dict[str, str] = {}
        for alias in aliases:
            if alias.kind == "ticker":
                if len(alias.key) >= 3 and alias.key not in GENERIC_TICKERS:
                    tickers.setdefault(alias.key.upper(), alias.symbol)
                continue
            key = alias.key.lower()
            if not key:
                continue
            if " " not in key and (key in GENERIC_NAME_KEYS or len(key) < 4):
                continue
            if key in name_seen:
                if name_seen[key] != alias.symbol:
                    name_seen[key] = None
                continue
            name_seen[key] = alias.symbol
        return cls(
            name_keys={key: symbol for key, symbol in name_seen.items() if symbol},
            tickers=tickers,
        )


def _normalised_words(text: str) -> list[str]:
    return _NAME_WORD.findall(text.lower().replace("&", " and "))


def strip_publisher(title: str) -> str:
    """Remove a trailing ' - Publisher' / ' | Publisher' suffix from a headline."""
    if not isinstance(title, str):
        return ""
    return _PUBLISHER.sub("", title).strip()


def match_symbols(text: str, index: AliasIndex) -> tuple[str, ...]:
    """Return up to ``MAX_SYMBOLS_PER_ITEM`` symbols mentioned in ``text``.

    Company-name hits are resolved before ticker hits, and the longest alias
    wins. That stops a parent ticker (``SIEMENS``) from being added when the
    headline names a longer subsidiary (Siemens Energy India), and stops the
    publisher suffix (``Business Standard``) from becoming a company.
    """
    if not isinstance(text, str) or not text.strip():
        return ()
    clean = strip_publisher(text)
    found: list[str] = []
    for pattern, symbol in _MANUAL_SYMBOLS:
        if pattern.search(clean) and symbol not in found:
            found.append(symbol)
    if len(found) >= MAX_SYMBOLS_PER_ITEM:
        return tuple(found)

    words = _normalised_words(clean)
    matched_keys: list[str] = []
    if words and index.name_keys:
        hits: list[tuple[int, int, str, str]] = []
        for size in range(min(MAX_ALIAS_WORDS, len(words)), 0, -1):
            for start in range(0, len(words) - size + 1):
                key = " ".join(words[start : start + size])
                symbol = index.name_keys.get(key)
                if symbol is not None:
                    hits.append((start, start + size, symbol, key))
        hits.sort(key=lambda hit: (-(hit[1] - hit[0]), hit[0]))
        taken: list[tuple[int, int]] = []
        for start, end, symbol, key in hits:
            if any(
                start < used_end and end > used_start for used_start, used_end in taken
            ):
                continue
            taken.append((start, end))
            matched_keys.append(key)
            if symbol not in found:
                found.append(symbol)
            if len(found) >= MAX_SYMBOLS_PER_ITEM:
                return tuple(found)

    name_words = {word for key in matched_keys for word in key.split()}
    for token in _TOKEN.findall(clean):
        if token != token.upper() or not any(char.isalpha() for char in token):
            continue
        candidate = token.strip("._-")
        if candidate.lower() in name_words:
            continue
        symbol = index.tickers.get(candidate)
        if symbol is not None and symbol not in found:
            found.append(symbol)
        if len(found) >= MAX_SYMBOLS_PER_ITEM:
            break
    return tuple(found)


def parse_amount_crore(text: str) -> Decimal:
    """Largest rupee amount in the text, expressed in crore. Zero when absent."""
    if not isinstance(text, str):
        return Decimal("0")
    best = Decimal("0")
    for pattern in (_MONEY, _AMOUNT_WITHOUT_SYMBOL):
        for match in pattern.finditer(text):
            raw = match.group(1).replace(",", "")
            try:
                amount = Decimal(raw)
            except InvalidOperation:
                continue
            unit = match.group(2).lower()
            if unit.startswith("lakh"):
                amount = amount / Decimal("100")
            elif unit.startswith("thousand"):
                amount = amount / Decimal("10000")
            if amount > best:
                best = amount
    return best


def _amount_bucket(crore: Decimal) -> int:
    if crore >= Decimal("1000"):
        return 3
    if crore >= Decimal("100"):
        return 2
    if crore > 0:
        return 1
    return 0


def _event_strength(
    rule: _Rule, text: str, *, published_on: date, trade_date: date
) -> int:
    strength = rule.base
    if rule.event.value in _MONEY_EVENTS:
        strength += _amount_bucket(parse_amount_crore(text))
    if published_on == trade_date:
        strength += 1
    return max(MIN_STRENGTH, min(MAX_STRENGTH, strength))


def _match_events(text: str) -> list[tuple[_Rule, str]]:
    matches: list[tuple[_Rule, str]] = []
    seen: set[CatalystEvent] = set()
    for rule in _RULES:
        if rule.event in seen:
            continue
        hit = rule.pattern.search(text)
        if hit is None:
            continue
        seen.add(rule.event)
        term = _WHITESPACE.sub(" ", hit.group(0))[:MAX_MATCHED_TERM_CHARS]
        matches.append((rule, term))
        if len(matches) >= MAX_EVENTS_PER_ITEM:
            break
    return matches


def _published_on(instant: datetime) -> date:
    return core_utc(instant).astimezone(EXCHANGE_TIMEZONE).date()


def extract(
    item: NewsItem,
    index: AliasIndex,
    *,
    trade_date: date,
) -> tuple[Catalyst, ...]:
    """Extract bounded, deterministic catalysts from one news item."""
    if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
        raise CatalystError("Trade date must be a calendar date.")
    if item.category not in {NewsCategory.CORPORATE, NewsCategory.REGULATORY}:
        return ()
    if _ROUNDUP.search(item.title):
        return ()
    text = f"{item.title} {item.summary}"
    lowered = text.lower()
    symbols = match_symbols(item.title, index)
    if not symbols:
        return ()

    matches = _match_events(lowered)
    published = _published_on(item.published_at)
    catalysts: list[Catalyst] = []
    for symbol in symbols:
        if not matches:
            catalysts.append(
                Catalyst(
                    news_id=item.news_id,
                    symbol=symbol,
                    event=CatalystEvent.CORPORATE_OTHER,
                    polarity=0,
                    strength=MIN_STRENGTH,
                    matched_terms=(),
                )
            )
            continue
        for rule, term in matches:
            catalysts.append(
                Catalyst(
                    news_id=item.news_id,
                    symbol=symbol,
                    event=rule.event,
                    polarity=POLARITY[rule.event],
                    strength=_event_strength(
                        rule,
                        lowered,
                        published_on=published,
                        trade_date=trade_date,
                    ),
                    matched_terms=(term,),
                )
            )
    return tuple(catalysts[: MAX_SYMBOLS_PER_ITEM * MAX_EVENTS_PER_ITEM])


def extract_many(
    items: Sequence[NewsItem],
    index: AliasIndex,
    *,
    trade_date: date,
) -> tuple[Catalyst, ...]:
    if len(items) > MAX_SCAN_ITEMS:
        raise CatalystError(
            f"At most {MAX_SCAN_ITEMS} news items can be scanned at once."
        )
    found: list[Catalyst] = []
    for item in items:
        found.extend(extract(item, index, trade_date=trade_date))
    return tuple(found)


def day_bounds(trade_date: date) -> tuple[datetime, datetime]:
    """UTC bounds for the IST trading day ``trade_date``."""
    if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
        raise CatalystError("Trade date must be a calendar date.")
    start = datetime.combine(trade_date, time.min, tzinfo=EXCHANGE_TIMEZONE)
    return (
        core_utc(start),
        core_utc(start + timedelta(days=1)),
    )


def _catalyst_id(news_id: str, symbol: str, event: CatalystEvent) -> str:
    digest = sha256(f"{news_id}\n{symbol}\n{event.value}".encode())
    return f"cat_{digest.hexdigest()[:32]}"


def _timestamp(value: datetime) -> str:
    return core_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


class CatalystStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=True)

    def record(
        self,
        catalysts: Iterable[Catalyst],
        *,
        trade_date: date,
        method: str = "DETERMINISTIC",
    ) -> int:
        if method not in {"DETERMINISTIC", "LLM"}:
            raise CatalystError("Unknown catalyst method.")
        if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
            raise CatalystError("Trade date must be a calendar date.")
        batch = tuple(catalysts)
        extracted_at = core_utc(self.clock.now())
        stamp = _timestamp(extracted_at)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                inserted = 0
                for item in batch:
                    cursor = connection.execute(
                        "INSERT INTO news_catalysts "
                        "(catalyst_id, news_id, symbol, event_type, polarity, "
                        "strength, matched_terms, method, trade_date, extracted_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (news_id, symbol, event_type) DO NOTHING",
                        (
                            _catalyst_id(item.news_id, item.symbol, item.event),
                            item.news_id,
                            item.symbol,
                            item.event.value,
                            item.polarity,
                            item.strength,
                            " | ".join(item.matched_terms),
                            method,
                            trade_date.isoformat(),
                            stamp,
                        ),
                    )
                    inserted += max(cursor.rowcount, 0)
                if inserted:
                    append_event(
                        connection,
                        occurred_at=extracted_at,
                        actor="owner",
                        action="research.catalysts.extracted",
                        object_type="news_catalysts",
                        object_id=trade_date.isoformat(),
                        payload={"inserted": str(inserted), "method": method},
                    )
        return inserted

    def for_date(self, trade_date: date) -> tuple[StoredCatalyst, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT c.catalyst_id, c.news_id, c.symbol, c.event_type, "
                "c.polarity, c.strength, c.matched_terms, c.trade_date, "
                "c.extracted_at, n.title, n.published_at "
                "FROM news_catalysts c JOIN news_items n ON n.news_id = c.news_id "
                "WHERE c.trade_date = ? "
                "ORDER BY c.symbol, c.strength DESC, n.published_at DESC",
                (trade_date.isoformat(),),
            ).fetchall()
        return tuple(
            StoredCatalyst(
                catalyst_id=str(row[0]),
                news_id=str(row[1]),
                symbol=str(row[2]),
                event=CatalystEvent(str(row[3])),
                polarity=int(row[4]),
                strength=int(row[5]),
                matched_terms=tuple(term for term in str(row[6]).split(" | ") if term),
                trade_date=date.fromisoformat(str(row[7])),
                extracted_at=datetime.fromisoformat(str(row[8]).replace("Z", "+00:00")),
                title=str(row[9]),
                published_at=datetime.fromisoformat(
                    str(row[10]).replace("Z", "+00:00")
                ),
            )
            for row in rows
        )

    def unmatched_corporate_count(
        self,
        trade_date: date,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """Corporate headlines in the window that produced no catalyst at all."""
        window_start, window_end = day_bounds(trade_date)
        start = window_start if start is None else start
        end = window_end if end is None else end
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM news_items n "
                "WHERE n.category IN ('CORPORATE', 'REGULATORY') "
                "AND n.published_at >= ? AND n.published_at < ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM news_catalysts c "
                "  WHERE c.news_id = n.news_id AND c.trade_date = ?"
                ")",
                (_timestamp(start), _timestamp(end), trade_date.isoformat()),
            ).fetchone()
        return int(row[0]) if row else 0


__all__ = [
    "AliasIndex",
    "Catalyst",
    "CatalystError",
    "CatalystEvent",
    "CatalystStore",
    "DEFAULT_EVENT_WEIGHTS",
    "NEGATIVE_EVENTS",
    "POLARITY",
    "POSITIVE_EVENTS",
    "StoredCatalyst",
    "day_bounds",
    "extract",
    "extract_many",
    "match_symbols",
    "parse_amount_crore",
]
