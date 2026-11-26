import secrets
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from bq.core import (
    canonical_digest as core_canonical_digest,
)


class AuditIntegrityError(Exception):
    pass


def _digest(value: object) -> str:
    return core_canonical_digest(value)


def append_event(
    connection: sqlite3.Connection,
    *,
    occurred_at: datetime,
    actor: str,
    action: str,
    object_type: str,
    object_id: str,
    payload: Mapping[str, str],
    config_version: str | None = None,
) -> None:
    if occurred_at.utcoffset() is None:
        raise ValueError("Audit event time must be timezone-aware.")
    timestamp = (
        occurred_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM audit_events"
        ).fetchone()[0]
    )
    previous = connection.execute(
        "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    previous_hash = str(previous[0]) if previous else None
    event_id = secrets.token_urlsafe(18)
    payload_hash = _digest(dict(payload))
    fields = {
        "sequence": sequence,
        "event_id": event_id,
        "occurred_at": timestamp,
        "actor": actor,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "config_version": config_version,
        "payload_hash": payload_hash,
        "previous_hash": previous_hash,
    }
    event_hash = _digest(fields)
    connection.execute(
        "INSERT INTO audit_events "
        "(sequence, event_id, occurred_at, actor, action, object_type, object_id, "
        "config_version, payload_hash, previous_hash, event_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sequence,
            event_id,
            timestamp,
            actor,
            action,
            object_type,
            object_id,
            config_version,
            payload_hash,
            previous_hash,
            event_hash,
        ),
    )


def verify_chain(connection: sqlite3.Connection) -> None:
    previous_hash: str | None = None
    rows = connection.execute(
        "SELECT sequence, event_id, occurred_at, actor, action, object_type, "
        "object_id, config_version, payload_hash, previous_hash, event_hash "
        "FROM audit_events ORDER BY sequence"
    )
    for row in rows:
        (
            sequence,
            event_id,
            occurred_at,
            actor,
            action,
            object_type,
            object_id,
            config_version,
            payload_hash,
            stored_previous_hash,
            event_hash,
        ) = row
        fields = {
            "sequence": sequence,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "actor": actor,
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "config_version": config_version,
            "payload_hash": payload_hash,
            "previous_hash": stored_previous_hash,
        }
        if stored_previous_hash != previous_hash or _digest(fields) != event_hash:
            raise AuditIntegrityError(
                f"Audit event chain is invalid at sequence {sequence}."
            )
        previous_hash = str(event_hash)
