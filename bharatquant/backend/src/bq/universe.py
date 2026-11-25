import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from re import compile as regex

from bq.core import Clock, CurrencyPaise, SystemClock
from bq.infra.audit import append_event

IDENTIFIER = regex(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class UniverseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UniverseVersion:
    universe_id: str
    version: int
    effective_date: date
    recorded_at: datetime
    minimum_market_cap_paise: int
    source_id: str
    source_url: str | None
    instrument_ids: tuple[str, ...]


class UniverseStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def publish(
        self,
        *,
        universe_id: str,
        effective_date: date,
        minimum_market_cap_paise: CurrencyPaise,
        source_id: str,
        source_url: str | None,
        market_caps: tuple[tuple[str, CurrencyPaise], ...],
    ) -> UniverseVersion:
        if (
            not isinstance(universe_id, str)
            or IDENTIFIER.fullmatch(universe_id) is None
        ):
            raise UniverseError("Invalid universe identifier.")
        if not isinstance(source_id, str) or IDENTIFIER.fullmatch(source_id) is None:
            raise UniverseError("Invalid universe source identifier.")
        if not isinstance(effective_date, date):
            raise UniverseError("Effective date must be a date.")
        if not isinstance(minimum_market_cap_paise, CurrencyPaise):
            raise TypeError("Minimum market cap must be CurrencyPaise.")
        if source_url is not None and (
            not isinstance(source_url, str) or len(source_url) > 2048
        ):
            raise UniverseError("Invalid universe source URL.")
        if not isinstance(market_caps, tuple) or len(market_caps) > 10000:
            raise UniverseError("Universe market caps must be a bounded tuple.")

        seen: set[str] = set()
        memberships: list[tuple[str, int]] = []
        for instrument_id, market_cap in market_caps:
            if (
                not isinstance(instrument_id, str)
                or IDENTIFIER.fullmatch(instrument_id) is None
            ):
                raise UniverseError("Invalid universe instrument identifier.")
            if not isinstance(market_cap, CurrencyPaise):
                raise UniverseError("Universe market cap must be CurrencyPaise.")
            if instrument_id in seen:
                raise UniverseError("Universe instrument identifiers must be unique.")
            seen.add(instrument_id)
            memberships.append((instrument_id, market_cap.value))

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM universe_versions "
                    "WHERE universe_id = ?",
                    (universe_id,),
                ).fetchone()
                version = int(row[0]) + 1
                recorded_at = self.clock.now()
                if recorded_at.utcoffset() is None:
                    raise UniverseError(
                        "Universe recording time must be timezone-aware."
                    )
                recorded_text = (
                    recorded_at.astimezone(UTC)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
                connection.execute(
                    "INSERT INTO universe_versions "
                    "(universe_id, version, effective_date, recorded_at, "
                    "minimum_market_cap_paise, source_id, source_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        universe_id,
                        version,
                        effective_date.isoformat(),
                        recorded_text,
                        minimum_market_cap_paise.value,
                        source_id,
                        source_url,
                    ),
                )
                connection.executemany(
                    "INSERT INTO universe_membership "
                    "(universe_id, version, instrument_id, market_cap_paise) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (universe_id, version, instrument_id, market_cap)
                        for instrument_id, market_cap in memberships
                    ],
                )
                append_event(
                    connection,
                    occurred_at=recorded_at,
                    actor="owner",
                    action="universe.version.published",
                    object_type="universe",
                    object_id=f"{universe_id}:{version}",
                    config_version=str(version),
                    payload={
                        "effective_date": effective_date.isoformat(),
                        "source_id": source_id,
                        "minimum_market_cap_paise": str(minimum_market_cap_paise.value),
                    },
                )
        return UniverseVersion(
            universe_id=universe_id,
            version=version,
            effective_date=effective_date,
            recorded_at=recorded_at.astimezone(UTC),
            minimum_market_cap_paise=minimum_market_cap_paise.value,
            source_id=source_id,
            source_url=source_url,
            instrument_ids=tuple(
                sorted(
                    instrument_id
                    for instrument_id, market_cap in memberships
                    if market_cap >= minimum_market_cap_paise.value
                )
            ),
        )

    def get(
        self,
        universe_id: str,
        *,
        effective_date: date,
        as_of: datetime | None = None,
    ) -> UniverseVersion:
        if (
            not isinstance(universe_id, str)
            or IDENTIFIER.fullmatch(universe_id) is None
        ):
            raise UniverseError("Invalid universe identifier.")
        if not isinstance(effective_date, date):
            raise UniverseError("Effective date must be a date.")
        cutoff = self.clock.now() if as_of is None else as_of
        if cutoff.utcoffset() is None:
            raise UniverseError("Universe as-of time must be timezone-aware.")
        cutoff_text = (
            cutoff.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT universe_id, version, effective_date, recorded_at, "
                "minimum_market_cap_paise, source_id, source_url "
                "FROM universe_versions "
                "WHERE universe_id = ? AND effective_date <= ? "
                "AND recorded_at <= ? "
                "ORDER BY effective_date DESC, version DESC LIMIT 1",
                (universe_id, effective_date.isoformat(), cutoff_text),
            ).fetchone()
            if row is None:
                raise UniverseError("Universe coverage is unavailable.")
            instrument_rows = connection.execute(
                "SELECT instrument_id FROM universe_membership "
                "WHERE universe_id = ? AND version = ? "
                "AND market_cap_paise >= ? "
                "ORDER BY instrument_id",
                (universe_id, int(row[1]), int(row[4])),
            ).fetchall()
            return UniverseVersion(
                universe_id=str(row[0]),
                version=int(row[1]),
                effective_date=date.fromisoformat(str(row[2])),
                recorded_at=datetime.fromisoformat(str(row[3]).replace("Z", "+00:00")),
                minimum_market_cap_paise=int(row[4]),
                source_id=str(row[5]),
                source_url=str(row[6]) if row[6] is not None else None,
                instrument_ids=tuple(str(item[0]) for item in instrument_rows),
            )
