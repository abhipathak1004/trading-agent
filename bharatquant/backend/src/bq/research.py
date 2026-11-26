import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from bq.core import (
    IDENTIFIER,
    Clock,
    SystemClock,
)
from bq.core import (
    canonical_digest as core_canonical_digest,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    parse_timestamp as core_parse_timestamp,
)
from bq.core import (
    utc as core_utc,
)
from bq.infra.audit import append_event


def _utc(value: datetime) -> datetime:
    try:
        return core_utc(value)
    except ValueError as exc:
        raise ResearchError("Research timestamps must be timezone-aware.") from exc


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return core_parse_timestamp(value)


class ResearchError(ValueError):
    pass


class ThesisHorizon(StrEnum):
    SHORT_TERM = "SHORT_TERM"
    SWING = "SWING"
    MEDIUM = "MEDIUM"
    LONG = "LONG"


class ThesisState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True, slots=True)
class Filing:
    filing_id: str
    instrument_id: str
    filing_type: str
    source_id: str
    published_at: datetime
    ingested_at: datetime
    filing_hash: str


@dataclass(frozen=True, slots=True)
class Thesis:
    thesis_id: str
    instrument_id: str
    horizon: ThesisHorizon
    state: ThesisState
    created_at: datetime
    thesis_hash: str


def _hash(payload: object) -> str:
    return core_canonical_digest(payload)


class ResearchStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=True)

    def freeze_filing(
        self,
        *,
        filing_id: str,
        instrument_id: str,
        filing_type: str,
        source_id: str,
        published_at: datetime,
        ingested_at: datetime,
    ) -> Filing:
        for value, label in (
            (filing_id, "filing"),
            (instrument_id, "instrument"),
            (source_id, "source"),
        ):
            if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
                raise ResearchError(f"Invalid {label} identifier.")
        if not isinstance(filing_type, str) or not 1 <= len(filing_type) <= 128:
            raise ResearchError("Invalid filing type.")
        published = _utc(published_at)
        ingested = _utc(ingested_at)
        if ingested < published:
            raise ResearchError("Filing cannot be ingested before publication.")
        payload = {
            "filing_id": filing_id,
            "instrument_id": instrument_id,
            "filing_type": filing_type,
            "source_id": source_id,
            "published_at": _timestamp(published),
            "ingested_at": _timestamp(ingested),
        }
        filing_hash = _hash(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT filing_hash FROM filings WHERE filing_id = ?",
                    (filing_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != filing_hash:
                        raise ResearchError("Filing identity has conflicting content.")
                    return self.get_filing(filing_id)
                connection.execute(
                    "INSERT INTO filings "
                    "(filing_id, instrument_id, filing_type, source_id, "
                    "published_at, ingested_at, filing_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        filing_id,
                        instrument_id,
                        filing_type,
                        source_id,
                        _timestamp(published),
                        _timestamp(ingested),
                        filing_hash,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=ingested,
                    actor="owner",
                    action="research.filing.frozen",
                    object_type="filing",
                    object_id=filing_id,
                    payload={"filing_hash": filing_hash},
                )
        return self.get_filing(filing_id)

    def create_thesis(
        self,
        *,
        thesis_id: str,
        instrument_id: str,
        horizon: ThesisHorizon,
    ) -> Thesis:
        for value, label in (
            (thesis_id, "thesis"),
            (instrument_id, "instrument"),
        ):
            if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
                raise ResearchError(f"Invalid {label} identifier.")
        if not isinstance(horizon, ThesisHorizon):
            raise ResearchError("Invalid thesis horizon.")
        created_at = _utc(self.clock.now())
        payload = {
            "thesis_id": thesis_id,
            "instrument_id": instrument_id,
            "horizon": horizon.value,
            "state": ThesisState.DRAFT.value,
            "created_at": _timestamp(created_at),
        }
        thesis_hash = _hash(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT thesis_hash FROM theses WHERE thesis_id = ?",
                    (thesis_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != thesis_hash:
                        raise ResearchError("Thesis identity has conflicting content.")
                    return self.get_thesis(thesis_id)
                connection.execute(
                    "INSERT INTO theses "
                    "(thesis_id, instrument_id, horizon, state, created_at, thesis_hash) "
                    "VALUES (?, ?, ?, 'DRAFT', ?, ?)",
                    (
                        thesis_id,
                        instrument_id,
                        horizon.value,
                        _timestamp(created_at),
                        thesis_hash,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=created_at,
                    actor="owner",
                    action="research.thesis.created",
                    object_type="thesis",
                    object_id=thesis_id,
                    payload={"thesis_hash": thesis_hash},
                )
        return self.get_thesis(thesis_id)

    def get_filing(self, filing_id: str) -> Filing:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT filing_id, instrument_id, filing_type, source_id, "
                "published_at, ingested_at, filing_hash "
                "FROM filings WHERE filing_id = ?",
                (filing_id,),
            ).fetchone()
        if row is None:
            raise ResearchError("Filing not found.")
        return Filing(
            filing_id=str(row[0]),
            instrument_id=str(row[1]),
            filing_type=str(row[2]),
            source_id=str(row[3]),
            published_at=_parse_timestamp(str(row[4])),
            ingested_at=_parse_timestamp(str(row[5])),
            filing_hash=str(row[6]),
        )

    def get_thesis(self, thesis_id: str) -> Thesis:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT thesis_id, instrument_id, horizon, state, created_at, thesis_hash "
                "FROM theses WHERE thesis_id = ?",
                (thesis_id,),
            ).fetchone()
        if row is None:
            raise ResearchError("Thesis not found.")
        return Thesis(
            thesis_id=str(row[0]),
            instrument_id=str(row[1]),
            horizon=ThesisHorizon(str(row[2])),
            state=ThesisState(str(row[3])),
            created_at=_parse_timestamp(
                str(row[4]),
            ),
            thesis_hash=str(row[5]),
        )
