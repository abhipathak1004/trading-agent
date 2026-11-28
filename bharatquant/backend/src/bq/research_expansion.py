"""Point-in-time research notes, thesis evidence, fundamental snapshots and claims."""

from __future__ import annotations

import secrets
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
    canonical_digest,
    open_connection,
    parse_timestamp,
    timestamp,
    utc,
)
from bq.fundamentals import (
    DcfResult,
    FundamentalRatios,
    FundamentalSnapshot,
    FundamentalSource,
    ValuationAssumptions,
    compute_ratios,
    dcf_valuation,
)
from bq.infra.audit import append_event
from bq.news import NewsItem
from bq.research import ResearchError, ResearchStore

MAX_TITLE = 512
MAX_BODY = 20_000
MAX_CLAIM = 2_000


class ResearchExpansionError(ValueError):
    pass


class NoteSource(StrEnum):
    NEWS = "NEWS"
    YOUTUBE = "YOUTUBE"
    XBRL = "XBRL"
    FILING = "FILING"
    OWNER = "OWNER"


@dataclass(frozen=True, slots=True)
class ResearchNote:
    note_id: str
    instrument_id: str | None
    source_type: NoteSource
    source_id: str
    url: str | None
    title: str
    body: str
    published_at: datetime
    known_at: datetime
    note_hash: str
    created_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "note_id": self.note_id,
            "instrument_id": self.instrument_id,
            "source_type": self.source_type.value,
            "source_id": self.source_id,
            "url": self.url,
            "title": self.title,
            "body": self.body,
            "published_at": self.published_at.isoformat(),
            "known_at": self.known_at.isoformat(),
            "note_hash": self.note_hash,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class FundamentalRecord:
    snapshot_id: str
    snapshot: FundamentalSnapshot
    snapshot_hash: str
    created_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            **self.snapshot.to_dict(),
            "snapshot_hash": self.snapshot_hash,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ThesisEventRecord:
    event_id: str
    thesis_id: str
    kind: str
    statement: str
    note_id: str | None
    occurred_at: datetime
    event_hash: str
    created_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "thesis_id": self.thesis_id,
            "kind": self.kind,
            "statement": self.statement,
            "note_id": self.note_id,
            "occurred_at": self.occurred_at.isoformat(),
            "event_hash": self.event_hash,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    claim_id: str
    instrument_id: str
    statement: str
    note_id: str | None
    claim_hash: str
    created_at: datetime
    status: str = "PENDING"
    review_note: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "instrument_id": self.instrument_id,
            "statement": self.statement,
            "note_id": self.note_id,
            "claim_hash": self.claim_hash,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "review_note": self.review_note,
        }


class ResearchExpansionStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.research = ResearchStore(path, clock)

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def create_note(
        self,
        *,
        source_type: NoteSource,
        source_id: str,
        title: str,
        body: str,
        published_at: datetime,
        known_at: datetime | None = None,
        instrument_id: str | None = None,
        url: str | None = None,
    ) -> ResearchNote:
        if not isinstance(source_type, NoteSource):
            raise ResearchExpansionError("Invalid research note source.")
        if not isinstance(source_id, str) or IDENTIFIER.fullmatch(source_id) is None:
            raise ResearchExpansionError("Invalid research source identifier.")
        if not isinstance(title, str) or not 1 <= len(title) <= MAX_TITLE:
            raise ResearchExpansionError("Research title is outside its bound.")
        if not isinstance(body, str) or not 1 <= len(body) <= MAX_BODY:
            raise ResearchExpansionError("Research body is outside its bound.")
        if instrument_id is not None and (
            not isinstance(instrument_id, str)
            or IDENTIFIER.fullmatch(instrument_id) is None
        ):
            raise ResearchExpansionError("Invalid instrument identifier.")
        if url is not None and (
            not isinstance(url, str)
            or not url.startswith("https://")
            or len(url) > 2_048
        ):
            raise ResearchExpansionError("Research URLs must use HTTPS.")
        published = utc(published_at)
        known = utc(known_at or self.clock.now())
        now = utc(self.clock.now())
        if known < published:
            raise ResearchExpansionError("Known-at cannot precede publication.")
        if known > now:
            raise ResearchExpansionError("Research cannot be known in the future.")
        note_id = "note_" + secrets.token_urlsafe(18)
        payload = {
            "note_id": note_id,
            "instrument_id": instrument_id,
            "source_type": source_type.value,
            "source_id": source_id,
            "url": url,
            "title": title,
            "body": body,
            "published_at": timestamp(published),
            "known_at": timestamp(known),
        }
        note_hash = canonical_digest(payload)
        created_at = now
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO research_notes "
                    "(note_id, instrument_id, source_type, source_id, url, title, "
                    "body, published_at, known_at, note_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        note_id,
                        instrument_id,
                        source_type.value,
                        source_id,
                        url,
                        title,
                        body,
                        timestamp(published),
                        timestamp(known),
                        note_hash,
                        timestamp(created_at),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=created_at,
                    actor="owner",
                    action="research.note.stored",
                    object_type="research_note",
                    object_id=note_id,
                    payload={
                        "source_type": source_type.value,
                        "note_hash": note_hash,
                    },
                )
        return self.get_note(note_id)

    def note_from_news(
        self, item: NewsItem, *, instrument_id: str | None = None
    ) -> ResearchNote:
        return self.create_note(
            source_type=NoteSource.NEWS,
            source_id=item.source_id,
            title=item.title,
            body=item.summary,
            published_at=item.published_at,
            known_at=item.ingested_at,
            instrument_id=instrument_id,
            url=item.url,
        )

    def get_note(self, note_id: str) -> ResearchNote:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT note_id, instrument_id, source_type, source_id, url, title, "
                "body, published_at, known_at, note_hash, created_at "
                "FROM research_notes WHERE note_id = ?",
                (note_id,),
            ).fetchone()
        if row is None:
            raise ResearchExpansionError("Research note not found.")
        return self._note_row(row)

    def notes(
        self,
        *,
        instrument_id: str | None = None,
        as_of: datetime | None = None,
        limit: int = 50,
    ) -> tuple[ResearchNote, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ResearchExpansionError("Note limit must be between 1 and 100.")
        clauses: list[str] = []
        params: list[object] = []
        if instrument_id is not None:
            clauses.append("instrument_id = ?")
            params.append(instrument_id)
        if as_of is not None:
            pivot = timestamp(utc(as_of))
            clauses.append("published_at <= ? AND known_at <= ?")
            params.extend([pivot, pivot])
        query = (
            "SELECT note_id, instrument_id, source_type, source_id, url, title, "
            "body, published_at, known_at, note_hash, created_at FROM research_notes "
            + ("WHERE " + " AND ".join(clauses) + " " if clauses else "")
            + "ORDER BY published_at DESC, note_id LIMIT ?"
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return tuple(self._note_row(row) for row in rows)

    def freeze_fundamentals(self, snapshot: FundamentalSnapshot) -> FundamentalRecord:
        if not isinstance(snapshot, FundamentalSnapshot):
            raise ResearchExpansionError("A fundamental snapshot is required.")
        snapshot_id = "fundamental_" + secrets.token_urlsafe(18)
        now = utc(self.clock.now())
        if snapshot.known_at > now:
            raise ResearchExpansionError("Fundamentals cannot be known in the future.")
        payload = {"snapshot_id": snapshot_id, **snapshot.to_dict()}
        snapshot_hash = canonical_digest(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO fundamental_snapshots "
                    "(snapshot_id, instrument_id, source, period_end, period_label, "
                    "known_at, revenue_paise, ebitda_paise, net_income_paise, "
                    "total_debt_paise, cash_paise, shares_outstanding, book_value_paise, "
                    "fcf_paise, snapshot_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        snapshot.instrument_id,
                        snapshot.source.value,
                        timestamp(snapshot.period_end),
                        snapshot.period_label,
                        timestamp(snapshot.known_at),
                        snapshot.revenue_paise,
                        snapshot.ebitda_paise,
                        snapshot.net_income_paise,
                        snapshot.total_debt_paise,
                        snapshot.cash_paise,
                        snapshot.shares_outstanding,
                        snapshot.book_value_paise,
                        snapshot.fcf_paise,
                        snapshot_hash,
                        timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="research.fundamentals.stored",
                    object_type="fundamental_snapshot",
                    object_id=snapshot_id,
                    payload={"snapshot_hash": snapshot_hash},
                )
        return self.get_fundamentals(snapshot_id)

    def get_fundamentals(self, snapshot_id: str) -> FundamentalRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT snapshot_id, instrument_id, source, period_end, period_label, "
                "known_at, revenue_paise, ebitda_paise, net_income_paise, "
                "total_debt_paise, cash_paise, shares_outstanding, book_value_paise, "
                "fcf_paise, snapshot_hash, created_at FROM fundamental_snapshots "
                "WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise ResearchExpansionError("Fundamental snapshot not found.")
        return self._fundamental_row(row)

    def fundamentals(
        self,
        instrument_id: str,
        *,
        as_of: datetime | None = None,
        limit: int = 20,
    ) -> tuple[FundamentalRecord, ...]:
        if not isinstance(instrument_id, str) or not IDENTIFIER.fullmatch(
            instrument_id
        ):
            raise ResearchExpansionError("Invalid instrument identifier.")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ResearchExpansionError("Fundamental limit must be between 1 and 50.")
        query = "SELECT snapshot_id FROM fundamental_snapshots WHERE instrument_id = ? "
        params: list[object] = [instrument_id]
        if as_of is not None:
            query += "AND known_at <= ? AND period_end <= ? "
            pivot = timestamp(utc(as_of))
            params.extend([pivot, pivot])
        query += "ORDER BY period_end DESC, snapshot_id DESC LIMIT ?"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return tuple(self.get_fundamentals(str(row[0])) for row in rows)

    def add_thesis_event(
        self,
        *,
        thesis_id: str,
        kind: str,
        statement: str,
        note_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> ThesisEventRecord:
        if kind not in {"EVIDENCE", "INVALIDATION", "REACTIVATION", "NOTE"}:
            raise ResearchExpansionError("Invalid thesis event kind.")
        if not isinstance(statement, str) or not 1 <= len(statement) <= MAX_CLAIM:
            raise ResearchExpansionError("Thesis statement is outside its bound.")
        if note_id is not None:
            self.get_note(note_id)
        try:
            self.research.get_thesis(thesis_id)
        except ResearchError as exc:
            raise ResearchExpansionError(str(exc)) from exc
        occurred = utc(occurred_at or self.clock.now())
        now = utc(self.clock.now())
        if occurred > now:
            raise ResearchExpansionError("Thesis events cannot be future-dated.")
        event_id = "thesis_event_" + secrets.token_urlsafe(18)
        event_hash = canonical_digest(
            {
                "event_id": event_id,
                "thesis_id": thesis_id,
                "kind": kind,
                "statement": statement,
                "note_id": note_id,
                "occurred_at": timestamp(occurred),
            }
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO thesis_events "
                    "(event_id, thesis_id, kind, statement, note_id, occurred_at, "
                    "event_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        thesis_id,
                        kind,
                        statement,
                        note_id,
                        timestamp(occurred),
                        event_hash,
                        timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="research.thesis.event",
                    object_type="thesis",
                    object_id=thesis_id,
                    payload={"kind": kind, "event_hash": event_hash},
                )
        return self.get_thesis_event(event_id)

    def get_thesis_event(self, event_id: str) -> ThesisEventRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT event_id, thesis_id, kind, statement, note_id, occurred_at, "
                "event_hash, created_at FROM thesis_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise ResearchExpansionError("Thesis event not found.")
        return ThesisEventRecord(
            event_id=str(row[0]),
            thesis_id=str(row[1]),
            kind=str(row[2]),
            statement=str(row[3]),
            note_id=str(row[4]) if row[4] is not None else None,
            occurred_at=parse_timestamp(str(row[5])),
            event_hash=str(row[6]),
            created_at=parse_timestamp(str(row[7])),
        )

    def thesis_events(self, thesis_id: str) -> tuple[ThesisEventRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT event_id FROM thesis_events WHERE thesis_id = ? "
                "ORDER BY occurred_at, event_id",
                (thesis_id,),
            ).fetchall()
        return tuple(self.get_thesis_event(str(row[0])) for row in rows)

    def create_claim(
        self,
        *,
        instrument_id: str,
        statement: str,
        note_id: str | None = None,
    ) -> ResearchClaim:
        if not isinstance(instrument_id, str) or not IDENTIFIER.fullmatch(
            instrument_id
        ):
            raise ResearchExpansionError("Invalid instrument identifier.")
        if not isinstance(statement, str) or not 1 <= len(statement) <= MAX_CLAIM:
            raise ResearchExpansionError("Claim statement is outside its bound.")
        if note_id is not None:
            self.get_note(note_id)
        claim_id = "claim_" + secrets.token_urlsafe(18)
        now = utc(self.clock.now())
        claim_hash = canonical_digest(
            {
                "claim_id": claim_id,
                "instrument_id": instrument_id,
                "statement": statement,
                "note_id": note_id,
            }
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO research_claims "
                    "(claim_id, instrument_id, statement, note_id, claim_hash, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        claim_id,
                        instrument_id,
                        statement,
                        note_id,
                        claim_hash,
                        timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="research.claim.created",
                    object_type="research_claim",
                    object_id=claim_id,
                    payload={"claim_hash": claim_hash},
                )
        return self.get_claim(claim_id)

    def get_claim(self, claim_id: str) -> ResearchClaim:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT claim_id, instrument_id, statement, note_id, claim_hash, "
                "created_at FROM research_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if row is None:
                raise ResearchExpansionError("Research claim not found.")
            review = connection.execute(
                "SELECT decision, note FROM research_claim_reviews "
                "WHERE claim_id = ? ORDER BY created_at DESC, review_id DESC LIMIT 1",
                (claim_id,),
            ).fetchone()
        return ResearchClaim(
            claim_id=str(row[0]),
            instrument_id=str(row[1]),
            statement=str(row[2]),
            note_id=str(row[3]) if row[3] is not None else None,
            claim_hash=str(row[4]),
            created_at=parse_timestamp(str(row[5])),
            status=str(review[0]) if review is not None else "PENDING",
            review_note=str(review[1]) if review is not None else None,
        )

    def claims(
        self,
        *,
        instrument_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> tuple[ResearchClaim, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ResearchExpansionError("Claim limit must be between 1 and 100.")
        clauses: list[str] = []
        params: list[object] = []
        if instrument_id is not None:
            clauses.append("c.instrument_id = ?")
            params.append(instrument_id)
        if status is not None:
            if status not in {"PENDING", "ACCEPTED", "REJECTED"}:
                raise ResearchExpansionError("Invalid claim status.")
            if status == "PENDING":
                clauses.append(
                    "NOT EXISTS (SELECT 1 FROM research_claim_reviews r "
                    "WHERE r.claim_id = c.claim_id)"
                )
            else:
                clauses.append(
                    "EXISTS (SELECT 1 FROM research_claim_reviews r "
                    "WHERE r.claim_id = c.claim_id AND r.decision = ? "
                    "AND r.created_at = (SELECT MAX(r2.created_at) "
                    "FROM research_claim_reviews r2 WHERE r2.claim_id = c.claim_id))"
                )
                params.append(status)
        query = (
            "SELECT c.claim_id FROM research_claims c "
            + ("WHERE " + " AND ".join(clauses) + " " if clauses else "")
            + "ORDER BY c.created_at DESC, c.claim_id LIMIT ?"
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return tuple(self.get_claim(str(row[0])) for row in rows)

    def review_claim(
        self, claim_id: str, *, decision: str, note: str = ""
    ) -> ResearchClaim:
        if decision not in {"ACCEPTED", "REJECTED"}:
            raise ResearchExpansionError("Claim decision must be ACCEPTED or REJECTED.")
        if not isinstance(note, str) or len(note) > 512:
            raise ResearchExpansionError("Claim review note is too long.")
        claim = self.get_claim(claim_id)
        if claim.status != "PENDING":
            raise ResearchExpansionError("Claim has already been reviewed.")
        now = utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO research_claim_reviews "
                    "(review_id, claim_id, decision, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        "claim_review_" + secrets.token_urlsafe(18),
                        claim_id,
                        decision,
                        note,
                        timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="research.claim.reviewed",
                    object_type="research_claim",
                    object_id=claim_id,
                    payload={"decision": decision},
                )
        return self.get_claim(claim_id)

    def dossier(
        self,
        instrument_id: str,
        *,
        as_of: datetime | None = None,
        price_paise: int | None = None,
        assumptions: ValuationAssumptions | None = None,
    ) -> dict[str, object]:
        pivot = as_of or self.clock.now()
        notes = self.notes(instrument_id=instrument_id, as_of=pivot, limit=50)
        filings = self.research.filings(
            instrument_id=instrument_id, as_of=pivot, limit=50
        )
        theses = self.research.theses(instrument_id=instrument_id, as_of=pivot)
        thesis_payload = []
        for thesis in theses:
            events = [
                event
                for event in self.thesis_events(thesis.thesis_id)
                if event.occurred_at <= pivot
            ]
            thesis_payload.append(
                {
                    "thesis_id": thesis.thesis_id,
                    "instrument_id": thesis.instrument_id,
                    "horizon": thesis.horizon.value,
                    "state": thesis.state.value,
                    "created_at": thesis.created_at.isoformat(),
                    "thesis_hash": thesis.thesis_hash,
                    "events": [item.to_dict() for item in events],
                }
            )
        fundamentals = self.fundamentals(instrument_id, as_of=pivot)
        latest = fundamentals[0].snapshot if fundamentals else None
        ratios: FundamentalRatios | None = None
        dcf: DcfResult | None = None
        if latest is not None and price_paise is not None:
            ratios = compute_ratios(latest, price_paise=price_paise)
            if latest.fcf_paise > 0:
                dcf = dcf_valuation(
                    latest, price_paise=price_paise, assumptions=assumptions
                )
        return {
            "instrument_id": instrument_id,
            "as_of": pivot.isoformat(),
            "notes": [item.to_dict() for item in notes],
            "filings": [
                {
                    "filing_id": item.filing_id,
                    "filing_type": item.filing_type,
                    "source_id": item.source_id,
                    "published_at": item.published_at.isoformat(),
                    "ingested_at": item.ingested_at.isoformat(),
                    "filing_hash": item.filing_hash,
                }
                for item in filings
            ],
            "theses": thesis_payload,
            "fundamentals": [item.to_dict() for item in fundamentals],
            "ratios": ratios.to_dict() if ratios is not None else None,
            "dcf": dcf.to_dict() if dcf is not None else None,
            "claims": [
                item.to_dict()
                for item in self.claims(instrument_id=instrument_id, limit=50)
            ],
        }

    @staticmethod
    def _note_row(row: tuple[object, ...]) -> ResearchNote:
        return ResearchNote(
            note_id=str(row[0]),
            instrument_id=str(row[1]) if row[1] is not None else None,
            source_type=NoteSource(str(row[2])),
            source_id=str(row[3]),
            url=str(row[4]) if row[4] is not None else None,
            title=str(row[5]),
            body=str(row[6]),
            published_at=parse_timestamp(str(row[7])),
            known_at=parse_timestamp(str(row[8])),
            note_hash=str(row[9]),
            created_at=parse_timestamp(str(row[10])),
        )

    @staticmethod
    def _fundamental_row(row: tuple[object, ...]) -> FundamentalRecord:
        snapshot = FundamentalSnapshot(
            instrument_id=str(row[1]),
            source=FundamentalSource(str(row[2])),
            period_end=parse_timestamp(str(row[3])),
            period_label=str(row[4]) if row[4] is not None else None,
            known_at=parse_timestamp(str(row[5])),
            revenue_paise=int(str(row[6])),
            ebitda_paise=int(str(row[7])),
            net_income_paise=int(str(row[8])),
            total_debt_paise=int(str(row[9])),
            cash_paise=int(str(row[10])),
            shares_outstanding=int(str(row[11])),
            book_value_paise=int(str(row[12])),
            fcf_paise=int(str(row[13])),
        )
        return FundamentalRecord(
            snapshot_id=str(row[0]),
            snapshot=snapshot,
            snapshot_hash=str(row[14]),
            created_at=parse_timestamp(str(row[15])),
        )


__all__ = [
    "FundamentalRecord",
    "NoteSource",
    "ResearchClaim",
    "ResearchExpansionError",
    "ResearchExpansionStore",
    "ResearchNote",
    "ThesisEventRecord",
]
