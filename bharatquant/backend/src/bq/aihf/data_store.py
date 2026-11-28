"""Point-in-time company facts and an earnings-surprise history.

Two small stores the fund engine reads, both append-only and both honest about
*when* they became knowable:

* :class:`CompanyFactsStore` — a name's sector, industry, company name and ISIN.
  The engine's sector cap reads a sector per name and **silently exempts a name
  with none**, so a name without a row is quietly uncapped on that axis. That is
  why this is a store the ingest job fills rather than an optional nicety.
* :class:`EarningsStore` — one row per filed quarter, carrying the *filing date*
  and the BEAT/MISS that the deterministic PEAD analyst fires on. The filing
  date, not the fiscal period end, is what keeps it point-in-time.

Fundamental snapshots themselves live in ``ResearchExpansionStore`` alongside the
rest of the research record; this module only adds the two things the engine
needs that were not there.

Storage conventions are the app's own (see ``bq/infra/database.py``): STRICT
tables, ISO-8601 ``Z`` timestamps, a sha256 payload hash, and append-only tables.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bq.core import IDENTIFIER, Clock, SystemClock, utc

MAX_SYMBOL_CHARS = 64
MAX_TEXT_CHARS = 512
MAX_FACTS = 5_000
MAX_EARNINGS = 10_000

_EPS_SURPRISE = frozenset({"BEAT", "MISS", "MEET"})


class AiHfDataError(ValueError):
    """A fact, earnings row or query is outside the bounds this store accepts."""


def _timestamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _symbol(value: str) -> str:
    if not isinstance(value, str):
        raise AiHfDataError("A symbol is required.")
    symbol = value.strip().upper()
    if not 1 <= len(symbol) <= MAX_SYMBOL_CHARS or IDENTIFIER.fullmatch(symbol) is None:
        raise AiHfDataError(f"Invalid symbol {value!r}.")
    return symbol


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AiHfDataError("Company attributes must be text.")
    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed[:MAX_TEXT_CHARS]


def _hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Company facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompanyFactsRecord:
    symbol: str
    company_name: str | None
    isin: str | None
    sector: str | None
    industry: str | None
    known_at: datetime
    source_id: str
    source_url: str
    payload_hash: str
    recorded_at: datetime

    def has_sector(self) -> bool:
        return self.sector is not None


class CompanyFactsStore:
    """One row per symbol: the newest attributes known by ``as_of``.

    Sector and industry are treated as slow-moving company attributes, which is
    the same documented point-in-time approximation the engine's own snapshot
    builder makes (``features/snapshot.py``). A row is not versioned per date;
    its ``known_at`` is when the app learned it.
    """

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        from bq.core import open_connection

        return open_connection(self.path, foreign_keys=True)

    def record(
        self,
        *,
        symbol: str,
        company_name: str | None,
        isin: str | None,
        sector: str | None,
        industry: str | None,
        known_at: datetime,
        source_id: str,
        source_url: str,
    ) -> CompanyFactsRecord:
        """Upsert a symbol's attributes, keeping ``known_at`` monotonic.

        A later ingest that knows less (Yahoo omits a sector for a delisted
        name) must not erase a sector the app already had: a missing new value
        keeps the stored one. Losing a sector silently disables a risk limit,
        so the merge is the safe direction.
        """
        symbol = _symbol(symbol)
        now = utc(self.clock.now())
        known = utc(known_at)
        if known > now:
            raise AiHfDataError("Company facts cannot be known in the future.")
        if not isinstance(source_id, str) or IDENTIFIER.fullmatch(source_id) is None:
            raise AiHfDataError("Invalid source identifier.")
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            raise AiHfDataError("A source URL must be HTTPS.")
        payload = {
            "symbol": symbol,
            "company_name": _text(company_name),
            "isin": _text(isin),
            "sector": _text(sector),
            "industry": _text(industry),
            "known_at": _timestamp(known),
            "source_id": source_id,
        }
        digest = _hash(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT company_name, isin, sector, industry, known_at "
                    "FROM company_facts WHERE symbol = ?",
                    (symbol,),
                ).fetchone()
                merged_name = payload["company_name"] or (
                    existing[0] if existing else None
                )
                merged_isin = payload["isin"] or (existing[1] if existing else None)
                merged_sector = payload["sector"] or (existing[2] if existing else None)
                merged_industry = payload["industry"] or (
                    existing[3] if existing else None
                )
                # Keep known_at monotonic for the row as a whole.
                stored_known = known
                if existing is not None:
                    previous = _parse_timestamp(str(existing[4]))
                    stored_known = max(previous, known)
                connection.execute(
                    "INSERT INTO company_facts "
                    "(symbol, company_name, isin, sector, industry, known_at, "
                    "source_id, source_url, payload_hash, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(symbol) DO UPDATE SET "
                    "company_name=excluded.company_name, isin=excluded.isin, "
                    "sector=excluded.sector, industry=excluded.industry, "
                    "known_at=excluded.known_at, source_id=excluded.source_id, "
                    "source_url=excluded.source_url, "
                    "payload_hash=excluded.payload_hash, recorded_at=excluded.recorded_at",
                    (
                        symbol,
                        merged_name,
                        merged_isin,
                        merged_sector,
                        merged_industry,
                        _timestamp(stored_known),
                        source_id,
                        source_url,
                        digest,
                        _timestamp(now),
                    ),
                )
        return CompanyFactsRecord(
            symbol=symbol,
            company_name=merged_name,
            isin=merged_isin,
            sector=merged_sector,
            industry=merged_industry,
            known_at=stored_known,
            source_id=source_id,
            source_url=source_url,
            payload_hash=digest,
            recorded_at=now,
        )

    def get(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> CompanyFactsRecord | None:
        """The symbol's facts, or None. With *as_of*, only rows known by then."""
        symbol = _symbol(symbol)
        query = (
            "SELECT symbol, company_name, isin, sector, industry, known_at, "
            "source_id, source_url, payload_hash, recorded_at "
            "FROM company_facts WHERE symbol = ?"
        )
        params: list[object] = [symbol]
        if as_of is not None:
            query += " AND known_at <= ?"
            params.append(_timestamp(as_of))
        with closing(self._connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return _facts_row(row) if row is not None else None

    def sectors(self, symbols: list[str]) -> dict[str, str | None]:
        """``symbol -> sector`` for every name asked about, missing rows included
        as None, so a caller can warn about the names a sector cap will skip."""
        cleaned = [_symbol(symbol) for symbol in symbols]
        if not cleaned:
            return {}
        if len(cleaned) > MAX_FACTS:
            raise AiHfDataError("Too many symbols in one facts lookup.")
        placeholders = ",".join("?" for _ in cleaned)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT symbol, sector FROM company_facts WHERE symbol IN ({placeholders})",
                cleaned,
            ).fetchall()
        found = {
            str(row[0]): (str(row[1]) if row[1] is not None else None) for row in rows
        }
        return {symbol: found.get(symbol) for symbol in cleaned}

    def count(self) -> int:
        with closing(self._connect()) as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM company_facts").fetchone()[0]
            )


def _facts_row(row: tuple[object, ...]) -> CompanyFactsRecord:
    return CompanyFactsRecord(
        symbol=str(row[0]),
        company_name=str(row[1]) if row[1] is not None else None,
        isin=str(row[2]) if row[2] is not None else None,
        sector=str(row[3]) if row[3] is not None else None,
        industry=str(row[4]) if row[4] is not None else None,
        known_at=_parse_timestamp(str(row[5])),
        source_id=str(row[6]),
        source_url=str(row[7]),
        payload_hash=str(row[8]),
        recorded_at=_parse_timestamp(str(row[9])),
    )


# ---------------------------------------------------------------------------
# Earnings events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EarningsEvent:
    symbol: str
    report_period: str
    filing_date: str
    source_type: str
    eps_actual: float | None
    eps_estimate: float | None
    eps_surprise: str | None
    known_at: datetime

    def surprise_or_derived(self) -> str | None:
        """The surprise, or one derived from the actual vs estimate when the
        vendor did not label it. None when neither number exists."""
        if self.eps_surprise in _EPS_SURPRISE:
            return self.eps_surprise
        if self.eps_actual is None or self.eps_estimate is None:
            return None
        if self.eps_actual > self.eps_estimate:
            return "BEAT"
        if self.eps_actual < self.eps_estimate:
            return "MISS"
        return "MEET"


class EarningsStore:
    """Filed quarterly earnings, keyed by (symbol, report_period, filing date).

    Append-only: a re-ingest of the same filing is ignored rather than updated,
    because a record already read by a backtest must not change under it. The
    engine asks for history *as of a date*, and a filing becomes visible on its
    ``known_at``, which is the filing date plus the statutory lag.
    """

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        from bq.core import open_connection

        return open_connection(self.path, foreign_keys=True)

    def record(
        self,
        *,
        symbol: str,
        report_period: str,
        filing_date: str,
        eps_actual: float | None,
        eps_estimate: float | None,
        eps_surprise: str | None,
        known_at: datetime,
        source_type: str = "RESULT",
        source_id: str = "yahoo.finance",
        source_url: str = "https://finance.yahoo.com",
    ) -> bool:
        """Insert one filing. Returns False when it was already recorded.

        *report_period* and *filing_date* are ISO dates (``YYYY-MM-DD``).
        """
        symbol = _symbol(symbol)
        period = _iso_date(report_period, "report period")
        filed = _iso_date(filing_date, "filing date")
        if eps_surprise is not None and eps_surprise not in _EPS_SURPRISE:
            raise AiHfDataError(f"Invalid EPS surprise {eps_surprise!r}.")
        if not isinstance(source_type, str) or not 1 <= len(source_type) <= 32:
            raise AiHfDataError("Invalid earnings source type.")
        now = utc(self.clock.now())
        known = utc(known_at)
        if known > now:
            raise AiHfDataError("An earnings event cannot be known in the future.")
        payload = {
            "symbol": symbol,
            "report_period": period,
            "filing_date": filed,
            "eps_actual": eps_actual,
            "eps_estimate": eps_estimate,
            "eps_surprise": eps_surprise,
            "source_type": source_type,
        }
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO earnings_events "
                    "(symbol, report_period, filing_date, source_type, eps_actual, "
                    "eps_estimate, eps_surprise, known_at, source_id, source_url, "
                    "payload_hash, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        symbol,
                        period,
                        filed,
                        source_type,
                        eps_actual,
                        eps_estimate,
                        eps_surprise,
                        _timestamp(known),
                        source_id,
                        source_url,
                        _hash(payload),
                        _timestamp(now),
                    ),
                )
                return cursor.rowcount > 0

    def history(
        self,
        symbol: str,
        *,
        as_of: datetime | None = None,
        limit: int = 12,
    ) -> tuple[EarningsEvent, ...]:
        """A symbol's filings, newest report period first, visible by *as_of*."""
        symbol = _symbol(symbol)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 40
        ):
            raise AiHfDataError("Earnings limit must be between 1 and 40.")
        query = (
            "SELECT symbol, report_period, filing_date, source_type, eps_actual, "
            "eps_estimate, eps_surprise, known_at FROM earnings_events "
            "WHERE symbol = ?"
        )
        params: list[object] = [symbol]
        if as_of is not None:
            query += " AND known_at <= ?"
            params.append(_timestamp(as_of))
        query += " ORDER BY report_period DESC, filing_date DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_earnings_row(row) for row in rows)

    def count(self) -> int:
        with closing(self._connect()) as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM earnings_events").fetchone()[0]
            )


def _iso_date(value: str, what: str) -> str:
    from datetime import date

    if not isinstance(value, str):
        raise AiHfDataError(f"An earnings {what} must be an ISO date.")
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError as exc:
        raise AiHfDataError(f"Invalid earnings {what} {value!r}.") from exc


def _earnings_row(row: tuple[object, ...]) -> EarningsEvent:
    return EarningsEvent(
        symbol=str(row[0]),
        report_period=str(row[1]),
        filing_date=str(row[2]),
        source_type=str(row[3]),
        eps_actual=float(str(row[4])) if row[4] is not None else None,
        eps_estimate=float(str(row[5])) if row[5] is not None else None,
        eps_surprise=str(row[6]) if row[6] is not None else None,
        known_at=_parse_timestamp(str(row[7])),
    )


__all__ = [
    "AiHfDataError",
    "CompanyFactsRecord",
    "CompanyFactsStore",
    "EarningsEvent",
    "EarningsStore",
]
