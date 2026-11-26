"""Immutable daily report archive."""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

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

MAX_BODY_CHARS = 200_000


class ReportError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReportRecord:
    report_id: str
    report_type: str
    report_date: date
    status: str
    title: str
    body_md: str
    body: dict[str, object]
    content_hash: str
    file_path: str | None
    created_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "report_type": self.report_type,
            "report_date": self.report_date.isoformat(),
            "status": self.status,
            "title": self.title,
            "body_md": self.body_md,
            "body": self.body,
            "content_hash": self.content_hash,
            "file_path": self.file_path,
            "created_at": self.created_at.isoformat(),
        }


class ReportStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def record(
        self,
        *,
        report_type: str,
        report_date: date,
        status: str,
        title: str,
        body_md: str,
        body: dict[str, object],
        report_id: str | None = None,
    ) -> ReportRecord:
        if report_type not in {"morning", "evening"}:
            raise ReportError("Unknown report type.")
        if status not in {"ON_TIME", "LATE", "FAILED"}:
            raise ReportError("Unknown report status.")
        if not isinstance(report_date, date) or isinstance(report_date, datetime):
            raise ReportError("A report date is required.")
        if not isinstance(title, str) or not 1 <= len(title) <= 200:
            raise ReportError("A report title is required.")
        if not isinstance(body_md, str) or not 1 <= len(body_md) <= MAX_BODY_CHARS:
            raise ReportError("Report body is empty or too large.")
        if not isinstance(body, dict):
            raise ReportError("Report JSON must be an object.")
        body_json = json.dumps(
            body, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(body_json) > MAX_BODY_CHARS:
            raise ReportError("Report JSON is too large.")
        created_at = utc(self.clock.now())
        chosen_id = report_id or ("report_" + secrets.token_urlsafe(18))
        if len(chosen_id) > 128 or not chosen_id.isascii():
            raise ReportError("Invalid report identifier.")
        content_hash = canonical_digest(
            {"type": report_type, "md": body_md, "json": body}
        )
        relative = (
            Path("reports")
            / f"{report_date.year:04d}"
            / f"{report_date.month:02d}"
            / f"{report_date.day:02d}"
            / f"{chosen_id}.md"
        )
        file_path = self.path.parent / relative
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(body_md, encoding="utf-8")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO reports "
                    "(report_id, report_type, report_date, status, title, body_md, "
                    "body_json, content_hash, file_path, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chosen_id,
                        report_type,
                        report_date.isoformat(),
                        status,
                        title,
                        body_md,
                        body_json,
                        content_hash,
                        str(file_path),
                        timestamp(created_at),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=created_at,
                    actor="system",
                    action="report.recorded",
                    object_type="report",
                    object_id=chosen_id,
                    payload={"report_type": report_type, "content_hash": content_hash},
                )
        return self.get(chosen_id)

    def get(self, report_id: str) -> ReportRecord:
        if not isinstance(report_id, str) or not report_id:
            raise ReportError("Invalid report identifier.")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT report_id, report_type, report_date, status, title, body_md, "
                "body_json, content_hash, file_path, created_at FROM reports "
                "WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        if row is None:
            raise ReportError("Report not found.")
        return ReportRecord(
            report_id=str(row[0]),
            report_type=str(row[1]),
            report_date=date.fromisoformat(str(row[2])),
            status=str(row[3]),
            title=str(row[4]),
            body_md=str(row[5]),
            body=json.loads(str(row[6])),
            content_hash=str(row[7]),
            file_path=str(row[8]) if row[8] is not None else None,
            created_at=parse_timestamp(str(row[9])),
        )

    def list(self, *, limit: int = 30) -> tuple[ReportRecord, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ReportError("Report limit must be between 1 and 100.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT report_id FROM reports ORDER BY created_at DESC, report_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self.get(str(row[0])) for row in rows)


__all__ = ["MAX_BODY_CHARS", "ReportError", "ReportRecord", "ReportStore"]
