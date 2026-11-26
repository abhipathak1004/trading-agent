"""Procedural memory: versioned, owner-editable playbooks.

A playbook is guidance for a strategy run, not executable policy. The owner can
refine the text; names, descriptions and the set of playbooks stay code-owned.
Every save appends a version, and reset writes the default text as a new version.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from bq.core import (
    Clock,
    SystemClock,
    canonical_digest,
    open_connection,
    parse_timestamp,
    timestamp,
    utc,
)
from bq.infra.audit import append_event

MAX_CONTENT_CHARS = 16_384
MAX_PLAYBOOK_ID_CHARS = 64
_PLAYBOOK_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ProceduralError(ValueError):
    pass


class MissingPlaybook(ProceduralError):
    pass


@dataclass(frozen=True, slots=True)
class PlaybookDefinition:
    playbook_id: str
    name: str
    description: str
    content: str


DEFINITIONS: tuple[PlaybookDefinition, ...] = (
    PlaybookDefinition(
        playbook_id="momentum_continuation",
        name="Momentum continuation",
        description=(
            "Prefer clean trend and momentum alignment with volume confirmation; "
            "avoid fighting a strong trend."
        ),
        content="""\
Momentum continuation guidance:
- Prefer candidates where the trend and momentum dimensions agree.
- Prefer heavier-than-usual volume behind the move.
- Treat stretched volatility as a reason to reduce conviction, not to chase.
- Do not create a BUY where the deterministic plan is HOLD.
- Keep the strongest ranked names; explain any rejection briefly.
""",
    ),
    PlaybookDefinition(
        playbook_id="defensive",
        name="Capital preservation",
        description=(
            "Favour the cleanest setups, fewer positions, and explicit event or "
            "volatility caution."
        ),
        content="""\
Capital preservation guidance:
- Prefer the cleanest trend and structure; downgrade noisy or conflicting states.
- Prefer fewer positions and explain why each accepted name is robust.
- Treat elevated volatility, event risk or a missing invalidation as caution.
- Do not create a BUY where the deterministic plan is HOLD.
- Be explicit about what would make the portfolio view wrong.
""",
    ),
    PlaybookDefinition(
        playbook_id="event_aware",
        name="Event-aware",
        description=(
            "Surface corporate-event and news risk prominently; prefer candidates "
            "whose catalysts are supported by retrievable evidence."
        ),
        content="""\
Event-aware guidance:
- Surface corporate-event and news risk before ranking a candidate.
- Prefer claims whose evidence ids are verified; downgrade unverified claims.
- Treat an unhedged earnings or corporate-action gap as a reason for caution.
- Do not create a BUY where the deterministic plan is HOLD.
- Explain the event risk that remains even for an accepted name.
""",
    ),
)

DEFINITIONS_BY_ID = {definition.playbook_id: definition for definition in DEFINITIONS}


@dataclass(frozen=True, slots=True)
class Playbook:
    playbook_id: str
    name: str
    description: str
    content: str
    version: int
    updated_at: datetime | None
    is_default: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "playbook_id": self.playbook_id,
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "version": self.version,
            "updated_at": (
                None if self.updated_at is None else self.updated_at.isoformat()
            ),
            "is_default": self.is_default,
        }


class PlaybookSource(Protocol):
    def get(self, playbook_id: str) -> Playbook: ...


def validate_playbook_id(playbook_id: str) -> PlaybookDefinition:
    if not isinstance(playbook_id, str) or _PLAYBOOK_ID.fullmatch(playbook_id) is None:
        raise MissingPlaybook("Unknown playbook.")
    definition = DEFINITIONS_BY_ID.get(playbook_id)
    if definition is None:
        raise MissingPlaybook("Unknown playbook.")
    return definition


def validate_content(value: object) -> str:
    if not isinstance(value, str):
        raise ProceduralError("Playbook content must be text.")
    content = value.strip()
    if not content:
        raise ProceduralError("Playbook content cannot be empty.")
    if len(content) > MAX_CONTENT_CHARS:
        raise ProceduralError(
            f"Playbook content must be at most {MAX_CONTENT_CHARS} characters."
        )
    if _CONTROL.search(content):
        raise ProceduralError("Playbook content contains a control character.")
    return content


class PlaybookStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def list(self) -> tuple[Playbook, ...]:
        rows = self._latest_rows()
        return tuple(
            self._to_playbook(definition, rows.get(definition.playbook_id))
            for definition in DEFINITIONS
        )

    def get(self, playbook_id: str) -> Playbook:
        definition = validate_playbook_id(playbook_id)
        row = self._latest_rows({playbook_id}).get(playbook_id)
        return self._to_playbook(definition, row)

    def save(self, playbook_id: str, *, content: str) -> Playbook:
        validate_playbook_id(playbook_id)
        content_text = validate_content(content)
        updated_at = utc(self.clock.now())
        digest = canonical_digest({"playbook_id": playbook_id, "content": content_text})
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM procedural_playbooks "
                    "WHERE playbook_id = ?",
                    (playbook_id,),
                ).fetchone()
                version = int(row[0]) + 1
                connection.execute(
                    "INSERT INTO procedural_playbooks "
                    "(playbook_id, version, content, updated_at, content_hash) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        playbook_id,
                        version,
                        content_text,
                        timestamp(updated_at),
                        digest,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="memory.playbook.saved",
                    object_type="procedural_playbook",
                    object_id=playbook_id,
                    payload={
                        "playbook_id": playbook_id,
                        "version": str(version),
                        "content_hash": digest,
                    },
                )
        return self.get(playbook_id)

    def reset(self, playbook_id: str) -> Playbook:
        definition = validate_playbook_id(playbook_id)
        return self.save(playbook_id, content=definition.content)

    def _latest_rows(
        self, ids: set[str] | None = None
    ) -> dict[str, tuple[int, str, str]]:
        query = (
            "SELECT p.playbook_id, p.version, p.content, p.updated_at "
            "FROM procedural_playbooks AS p WHERE p.version = ("
            "SELECT MAX(q.version) FROM procedural_playbooks AS q "
            "WHERE q.playbook_id = p.playbook_id)"
        )
        params: tuple[object, ...] = ()
        if ids:
            placeholders = ",".join("?" for _ in ids)
            query = (
                "SELECT p.playbook_id, p.version, p.content, p.updated_at "
                "FROM procedural_playbooks AS p WHERE p.version = ("
                "SELECT MAX(q.version) FROM procedural_playbooks AS q "
                f"WHERE q.playbook_id = p.playbook_id) "
                f"AND p.playbook_id IN ({placeholders})"
            )
            params = tuple(sorted(ids))
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return {str(row[0]): (int(row[1]), str(row[2]), str(row[3])) for row in rows}

    @staticmethod
    def _to_playbook(
        definition: PlaybookDefinition,
        row: tuple[int, str, str] | None,
    ) -> Playbook:
        if row is None:
            return Playbook(
                playbook_id=definition.playbook_id,
                name=definition.name,
                description=definition.description,
                content=definition.content.strip(),
                version=0,
                updated_at=None,
                is_default=True,
            )
        version, content, updated = row
        try:
            updated_at = parse_timestamp(updated)
        except ValueError:
            updated_at = None
        return Playbook(
            playbook_id=definition.playbook_id,
            name=definition.name,
            description=definition.description,
            content=content,
            version=version,
            updated_at=updated_at,
            is_default=content == definition.content.strip(),
        )


__all__ = [
    "DEFINITIONS",
    "DEFINITIONS_BY_ID",
    "MAX_CONTENT_CHARS",
    "MissingPlaybook",
    "Playbook",
    "PlaybookDefinition",
    "PlaybookSource",
    "PlaybookStore",
    "ProceduralError",
    "validate_content",
    "validate_playbook_id",
]
