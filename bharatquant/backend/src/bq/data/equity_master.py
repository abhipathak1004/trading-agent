"""NSE equity listing, versioned, plus the alias index news attribution needs.

The Kite instrument master carries tokens and tickers but no company names, so a
headline saying "Sun Pharmaceutical" cannot be tied to ``SUNPHARMA``. This module
stores the published NSE listing (symbol / company name / ISIN) and derives the
deterministic alias keys the catalyst extractor matches against.

Rows are versioned by the date they were fetched and are immutable once written;
a refresh on a new day is a new version, never an update. That keeps the
news→symbol step reproducible: re-running yesterday's extraction uses yesterday's
names.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from bq.core import (
    Clock,
    SystemClock,
    exchange_time,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    utc as core_utc,
)
from bq.infra.audit import append_event
from bq.infra.equity_listing import NSE_EQUITY_LIST_URL

MAX_LISTING_ROWS = 5000
MAX_COMPANY_NAME_CHARS = 200
MAX_ISIN_CHARS = 32
MAX_SOURCE_URL_CHARS = 2048

# The same ticker shape the analysis pipeline can resolve. Symbols carrying
# characters the resolver rejects (``M&M``, for example) are left out rather than
# stored as candidates that could never be analysed.
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,23}")
_WORD = re.compile(r"[a-z0-9]+")

# Legal-form words that carry no identity. Deliberately narrow: removing "india"
# would collapse "Bank of India" into "bank", which is worse than missing it.
_SUFFIX_WORDS = frozenset(
    {
        "ltd",
        "limited",
        "corporation",
        "corp",
        "company",
        "co",
        "inc",
        "plc",
        "llp",
        "industries",
        "industry",
        "enterprises",
        "enterprise",
        "holdings",
        "holding",
        "the",
    }
)

# Words that are too generic to be a company-name prefix on their own.
_GENERIC_WORDS = frozenset({"of", "and", "the", "for"})
_LEGAL_WORDS = frozenset(
    {"ltd", "limited", "corporation", "corp", "company", "co", "inc", "plc", "llp"}
)


class EquityMasterError(ValueError):
    """The listing is malformed or the stored version does not exist."""


@dataclass(frozen=True, slots=True)
class EquityRow:
    symbol: str
    company_name: str
    series: str
    isin: str


@dataclass(frozen=True, slots=True)
class EquityAlias:
    """One deterministic key -> symbol mapping.

    ``kind`` is ``"name"`` for a normalised company-name key (matched
    case-insensitively) or ``"ticker"`` for the trading symbol (matched as an
    uppercase token).
    """

    key: str
    symbol: str
    kind: str


def normalise_company_name(name: str) -> str:
    """Lower-case a company name and strip punctuation and legal-form words."""
    if not isinstance(name, str):
        raise EquityMasterError("Company name must be text.")
    lowered = name.lower().replace("&", " and ")
    words = [word for word in _WORD.findall(lowered) if word not in _SUFFIX_WORDS]
    return " ".join(words)


def _raw_words(name: str) -> list[str]:
    """Company words with only legal forms removed, brand words kept."""
    lowered = name.lower().replace("&", " and ")
    return [word for word in _WORD.findall(lowered) if word not in _LEGAL_WORDS]


def _alias_keys(row: EquityRow) -> tuple[str, ...]:
    normalised = normalise_company_name(row.company_name)
    keys: list[str] = []
    if normalised:
        words = normalised.split()
        if len(normalised) >= 4 and (len(words) >= 2 or len(normalised) >= 6):
            keys.append(normalised)
        identity = [word for word in words if word not in _GENERIC_WORDS]
        if len(identity) >= 2:
            prefix = " ".join(identity[:2])
            if prefix != normalised and len(prefix) >= 8:
                keys.append(prefix)
    raw = _raw_words(row.company_name)
    if raw:
        raw_full = " ".join(raw)
        if raw_full != normalised and len(raw_full) >= 6:
            keys.append(raw_full)
        raw_identity = [word for word in raw if word not in _GENERIC_WORDS]
        if len(raw_identity) >= 2:
            raw_prefix = " ".join(raw_identity[:2])
            if raw_prefix not in {raw_full, normalised} and len(raw_prefix) >= 8:
                keys.append(raw_prefix)
    return tuple(dict.fromkeys(keys))


def parse_listing(payload: bytes) -> tuple[EquityRow, ...]:
    """Parse NSE's EQUITY_L.csv into bounded, resolvable equity rows."""
    if not isinstance(payload, bytes):
        raise EquityMasterError("Listing payload must be bytes.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EquityMasterError("Listing payload is not UTF-8 text.") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise EquityMasterError("Listing payload is empty.") from exc
    columns = {name.strip().upper(): index for index, name in enumerate(header)}
    required = ("SYMBOL", "NAME OF COMPANY", "SERIES", "ISIN NUMBER")
    for name in required:
        if name not in columns:
            raise EquityMasterError(f"Listing is missing the '{name}' column.")

    rows: list[EquityRow] = []
    seen: set[str] = set()
    for raw in reader:
        if len(raw) <= max(columns[name] for name in required):
            continue
        symbol = raw[columns["SYMBOL"]].strip().upper()
        company = " ".join(raw[columns["NAME OF COMPANY"]].split())
        series = raw[columns["SERIES"]].strip().upper()
        isin = raw[columns["ISIN NUMBER"]].strip().upper()
        if not symbol or not company or series != "EQ":
            continue
        if _SYMBOL.fullmatch(symbol) is None:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        rows.append(
            EquityRow(
                symbol=symbol,
                company_name=company[:MAX_COMPANY_NAME_CHARS],
                series=series,
                isin=isin[:MAX_ISIN_CHARS],
            )
        )
        if len(rows) >= MAX_LISTING_ROWS:
            break
    if not rows:
        raise EquityMasterError("Listing contained no equity rows.")
    return tuple(rows)


class EquityMasterStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=True)

    def refresh(
        self,
        payload: bytes,
        *,
        as_of: date,
        source_url: str = NSE_EQUITY_LIST_URL,
    ) -> int:
        if not isinstance(as_of, date) or isinstance(as_of, datetime):
            raise EquityMasterError("Listing date must be a calendar date.")
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            raise EquityMasterError("Listing source must be an HTTPS URL.")
        if len(source_url) > MAX_SOURCE_URL_CHARS:
            raise EquityMasterError("Listing source URL is too long.")
        rows = parse_listing(payload)
        fetched_at = core_utc(self.clock.now())
        stamp = fetched_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                inserted = 0
                for row in rows:
                    cursor = connection.execute(
                        "INSERT INTO equity_master "
                        "(symbol, company_name, series, isin, as_of_date, "
                        "source_url, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (symbol, as_of_date) DO NOTHING",
                        (
                            row.symbol,
                            row.company_name,
                            row.series,
                            row.isin,
                            as_of.isoformat(),
                            source_url,
                            stamp,
                        ),
                    )
                    inserted += cursor.rowcount
                append_event(
                    connection,
                    occurred_at=fetched_at,
                    actor="owner",
                    action="research.equity_master.refreshed",
                    object_type="equity_master",
                    object_id=as_of.isoformat(),
                    payload={
                        "parsed": str(len(rows)),
                        "inserted": str(inserted),
                        "source_url": source_url,
                    },
                )
        return inserted

    def latest_as_of(self) -> date | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT MAX(as_of_date) FROM equity_master"
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return date.fromisoformat(str(row[0]))

    def rows(self, *, as_of: date | None = None) -> tuple[EquityRow, ...]:
        pivot = as_of.isoformat() if as_of is not None else None
        query = (
            "SELECT symbol, company_name, series, isin FROM equity_master WHERE "
            "as_of_date = (SELECT MAX(as_of_date) FROM equity_master"
        )
        params: list[object] = []
        if pivot is not None:
            query += " WHERE as_of_date <= ?"
            params.append(pivot)
        query += ") ORDER BY symbol"
        with closing(self._connect()) as connection:
            fetched = connection.execute(query, params).fetchall()
        return tuple(
            EquityRow(
                symbol=str(row[0]),
                company_name=str(row[1]),
                series=str(row[2]),
                isin=str(row[3]),
            )
            for row in fetched
        )

    def aliases(self, *, as_of: date | None = None) -> tuple[EquityAlias, ...]:
        """Alias keys, longest first so a full name wins over a two-word prefix."""
        aliases: list[EquityAlias] = []
        for row in self.rows(as_of=as_of):
            for key in _alias_keys(row):
                aliases.append(EquityAlias(key=key, symbol=row.symbol, kind="name"))
            aliases.append(
                EquityAlias(key=row.symbol, symbol=row.symbol, kind="ticker")
            )
        aliases.sort(key=lambda item: (-len(item.key), item.key, item.symbol))
        return tuple(aliases)

    def search(
        self, query: str, *, as_of: date | None = None, limit: int = 50
    ) -> tuple[EquityRow, ...]:
        if not isinstance(query, str) or not query.strip():
            raise EquityMasterError("A search query is required.")
        if len(query) > 64:
            raise EquityMasterError("Search query is too long.")
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise EquityMasterError("Search limit must be between 1 and 100.")
        pivot = as_of.isoformat() if as_of is not None else None
        clauses = ["as_of_date = (SELECT MAX(as_of_date) FROM equity_master"]
        params: list[object] = []
        if pivot is not None:
            clauses[0] += " WHERE as_of_date <= ?"
            params.append(pivot)
        clauses[0] += ")"
        clauses.append("(UPPER(symbol) LIKE ? OR LOWER(company_name) LIKE ?)")
        needle = query.strip()
        params.extend([f"%{needle.upper()}%", f"%{needle.lower()}%"])
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT symbol, company_name, series, isin FROM equity_master WHERE "
                + " AND ".join(clauses)
                + " ORDER BY symbol LIMIT ?",
                params,
            ).fetchall()
        return tuple(
            EquityRow(
                symbol=str(row[0]),
                company_name=str(row[1]),
                series=str(row[2]),
                isin=str(row[3]),
            )
            for row in rows
        )

    def count(self, *, as_of: date | None = None) -> int:
        pivot = as_of.isoformat() if as_of is not None else None
        query = "SELECT COUNT(*) FROM equity_master WHERE as_of_date = (SELECT MAX(as_of_date) FROM equity_master"
        params: list[object] = []
        if pivot is not None:
            query += " WHERE as_of_date <= ?"
            params.append(pivot)
        query += ")"
        with closing(self._connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return int(row[0]) if row else 0


def listing_date(instant: datetime) -> date:
    """The IST calendar date a fetch belongs to."""
    return exchange_time(instant).date()


__all__ = [
    "EquityAlias",
    "EquityMasterError",
    "EquityMasterStore",
    "EquityRow",
    "MAX_LISTING_ROWS",
    "listing_date",
    "normalise_company_name",
    "parse_listing",
]
