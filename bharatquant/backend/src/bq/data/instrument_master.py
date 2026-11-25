"""Kite's instrument master, cached once per trading day.

``kite.instruments("NSE")`` returns tens of thousands of rows. Calling it per
request would be slow and would burn the broker's rate limit, so the dump is
stored once per calendar day and read from SQLite afterwards.

Deliberately not a "best effort" cache: if today's refresh fails, the previous
day's tokens are **not** served silently, because a stale token can point at a
different instrument after a symbol change. The failure surfaces as
``VendorUnavailableError`` and the caller decides whether yesterday's mapping is
acceptable.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from bq.core import Clock, SystemClock
from bq.data.symbols import Symbol
from bq.data.vendors.errors import NoMarketDataError, VendorUnavailableError

MIN_TOKEN = 1
MAX_TOKEN = 2**31 - 1


class InstrumentMasterError(ValueError):
    """Raised for malformed instrument rows."""


@dataclass(frozen=True, slots=True)
class Instrument:
    exchange: str
    tradingsymbol: str
    instrument_token: int
    lot_size: int

    def __post_init__(self) -> None:
        if self.exchange not in {"NSE", "BSE"}:
            raise InstrumentMasterError("Exchange must be NSE or BSE.")
        if not self.tradingsymbol or len(self.tradingsymbol) > 48:
            raise InstrumentMasterError("Trading symbol is missing or too long.")
        if not MIN_TOKEN <= self.instrument_token <= MAX_TOKEN:
            raise InstrumentMasterError("Instrument token is out of range.")
        if self.lot_size <= 0:
            raise InstrumentMasterError("Lot size must be positive.")


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise InstrumentMasterError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _parse_name(row: object, key: str) -> str:
    if not isinstance(row, dict):
        raise InstrumentMasterError("Instrument rows must be mappings.")
    value = row.get(key)
    if not isinstance(value, str):
        raise InstrumentMasterError(f"Instrument field '{key}' must be text.")
    return value


def _parse_int(row: dict[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InstrumentMasterError(f"Instrument field '{key}' must be an integer.")
    return value


def parse_instruments(rows: object, *, exchange: str) -> tuple[Instrument, ...]:
    """Normalise a Kite instrument dump into validated rows.

    Only equity-like rows are kept: anything with a non-empty ``instrument_type``
    that is not ``EQ`` (futures, options, currencies) is skipped, so the cache
    does not silently grow and a lookup cannot accidentally return an F&O token
    for an equity ticker.
    """
    if not isinstance(rows, list):
        raise InstrumentMasterError("Instrument dump must be a list.")
    instruments: list[Instrument] = []
    for row in rows:
        if not isinstance(row, dict):
            raise InstrumentMasterError("Instrument rows must be mappings.")
        row_exchange = _parse_name(row, "exchange")
        if row_exchange not in {"NSE", "BSE"}:
            continue
        instrument_type = row.get("instrument_type") or "EQ"
        if not isinstance(instrument_type, str):
            raise InstrumentMasterError("instrument_type must be text.")
        if instrument_type.upper() not in {"EQ", ""}:
            continue
        symbol = _parse_name(row, "tradingsymbol")
        instruments.append(
            Instrument(
                exchange=row_exchange,
                tradingsymbol=symbol,
                instrument_token=_parse_int(row, "instrument_token"),
                lot_size=_parse_int(row, "lot_size"),
            )
        )
    if exchange not in {"NSE", "BSE"}:
        raise InstrumentMasterError("Exchange must be NSE or BSE.")
    return tuple(instruments)


class InstrumentMasterStore:
    """Reads and writes the per-day instrument cache."""

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )

    def cached_date(self, exchange: str) -> date | None:
        """The most recent date we hold tokens for, or ``None``."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT MAX(as_of_date) FROM instrument_master WHERE exchange = ?",
                (exchange,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return date.fromisoformat(str(row[0]))

    def refresh(self, exchange: str, rows: object) -> int:
        """Replace ``exchange``'s tokens with today's dump. Returns rows stored."""
        if exchange not in {"NSE", "BSE"}:
            raise InstrumentMasterError("Exchange must be NSE or BSE.")
        instruments = parse_instruments(rows, exchange=exchange)
        fetched_at = _utc(self.clock.now())
        as_of = fetched_at.date().isoformat()
        stored = [item for item in instruments if item.exchange == exchange]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO instrument_master "
                    "(as_of_date, exchange, tradingsymbol, instrument_token, "
                    "lot_size, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            as_of,
                            item.exchange,
                            item.tradingsymbol,
                            item.instrument_token,
                            item.lot_size,
                            fetched_at.isoformat(timespec="microseconds").replace(
                                "+00:00", "Z"
                            ),
                        )
                        for item in stored
                    ],
                )
        return len(stored)

    def resolve(self, symbol: Symbol) -> Instrument:
        """Look up one instrument using the most recent cached dump."""
        as_of = self.cached_date(symbol.exchange.value)
        if as_of is None:
            raise VendorUnavailableError(
                "instrument_master_missing",
                f"No instrument cache for {symbol.exchange.value}.",
            )
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT exchange, tradingsymbol, instrument_token, lot_size "
                "FROM instrument_master "
                "WHERE as_of_date = ? AND exchange = ? AND tradingsymbol = ?",
                (as_of.isoformat(), symbol.exchange.value, symbol.tradingsymbol),
            ).fetchone()
        if row is None:
            raise NoMarketDataError(symbol.canonical)
        return Instrument(
            exchange=str(row[0]),
            tradingsymbol=str(row[1]),
            instrument_token=int(row[2]),
            lot_size=int(row[3]),
        )


__all__ = [
    "Instrument",
    "InstrumentMasterError",
    "InstrumentMasterStore",
    "parse_instruments",
]
