"""SQLite repositories for the Research Lab (migration 043).

One store per aggregate, the convention used across this codebase: the class
lives beside its domain, returns frozen dataclasses with ``to_dict()``, unpacks
rows positionally, and carries the shared ``_connect``/``BEGIN IMMEDIATE`` idiom.

Two disciplines are worth naming here:

* ``research_runs`` is mutable but not deletable (status transitions), while
  sessions, artifacts, snapshots and evidence are immutable once written.
* ``research_artifacts_fts`` is a *derived cache*, not authority. It is created
  lazily at runtime because stock SQLite builds may lack FTS5, and search falls
  back to a deterministic ``LIKE`` scan. This mirrors ``semantic_docs_fts``.
"""

from __future__ import annotations

import difflib
import json
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bq.core import Clock, SystemClock, open_connection, parse_timestamp, timestamp
from bq.infra.audit import append_event

FTS_TABLE = "research_artifacts_fts"
RUN_STATUSES = frozenset(
    {"queued", "running", "completed", "cancelled", "failed", "blocked"}
)
MAX_TITLE_CHARS = 200
MAX_PROMPT_CHARS = 20000
MAX_BODY_CHARS = 200000
MAX_QUERY_CHARS = 200


class ResearchLabError(ValueError):
    """The requested Research Lab operation is invalid."""


def _bounded(value: str, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchLabError(f"{name} must be a non-empty string.")
    if len(value) > limit:
        raise ResearchLabError(f"{name} must be at most {limit} characters.")
    return value


def _dumps(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ResearchLabError("A stored payload must be JSON-serialisable.") from exc


def _mapping(raw: object) -> dict[str, object]:
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _terms(query: str) -> list[str]:
    bounded = _bounded(query, "Search query", MAX_QUERY_CHARS)
    terms = [
        term
        for term in "".join(
            character if character.isalnum() else " " for character in bounded
        ).split()
        if term
    ]
    return terms[:12]


def _connect(path: Path) -> sqlite3.Connection:
    return open_connection(path, foreign_keys=True)


@dataclass(frozen=True, slots=True)
class ResearchSessionRecord:
    session_id: str
    title: str
    model_overrides: Mapping[str, object]
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "model_overrides": dict(self.model_overrides),
            "created_at_utc": timestamp(self.created_at),
            "updated_at_utc": timestamp(self.updated_at),
        }


class ResearchSessionStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return _connect(self.path)

    def create(
        self,
        *,
        title: str,
        model_overrides: Mapping[str, object] | None = None,
    ) -> ResearchSessionRecord:
        session_id = "sess_" + secrets.token_urlsafe(18)
        now = self.clock.now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO research_sessions "
                    "(session_id, title, model_overrides_json, created_at_utc, "
                    "updated_at_utc) VALUES (?, ?, ?, ?, ?)",
                    (
                        session_id,
                        _bounded(title, "Session title", MAX_TITLE_CHARS),
                        _dumps(model_overrides or {}),
                        timestamp(now),
                        timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="research.session.created",
                    object_type="research_session",
                    object_id=session_id,
                    payload={"title": title},
                )
        return self.get(session_id)

    def get(self, session_id: str) -> ResearchSessionRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT session_id, title, model_overrides_json, created_at_utc, "
                "updated_at_utc FROM research_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise ResearchLabError("Research session not found.")
        return ResearchSessionRecord(
            session_id=str(row[0]),
            title=str(row[1]),
            model_overrides=_mapping(row[2]),
            created_at=parse_timestamp(str(row[3])),
            updated_at=parse_timestamp(str(row[4])),
        )

    def list(self, *, limit: int = 50) -> tuple[ResearchSessionRecord, ...]:
        if not 1 <= limit <= 200:
            raise ResearchLabError("Session limit must be between 1 and 200.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT session_id FROM research_sessions "
                "ORDER BY updated_at_utc DESC, session_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self.get(str(row[0])) for row in rows)

    def touch(self, session_id: str) -> ResearchSessionRecord:
        now = self.clock.now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "UPDATE research_sessions SET updated_at_utc = ? "
                    "WHERE session_id = ?",
                    (timestamp(now), session_id),
                )
        return self.get(session_id)


@dataclass(frozen=True, slots=True)
class ResearchRunRecord:
    run_id: str
    session_id: str
    parent_run_id: str | None
    status: str
    prompt: str
    model_config: Mapping[str, object]
    params: Mapping[str, object]
    data_manifest_sha256: str | None
    frozen_data_path: str | None
    metrics: Mapping[str, object]
    validation: Mapping[str, object]
    usage: Mapping[str, object]
    artifact_dir: str | None
    created_at: datetime
    completed_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "parent_run_id": self.parent_run_id,
            "status": self.status,
            "prompt": self.prompt,
            "model_config": dict(self.model_config),
            "params": dict(self.params),
            "data_manifest_sha256": self.data_manifest_sha256,
            "frozen_data_path": self.frozen_data_path,
            "metrics": dict(self.metrics),
            "validation": dict(self.validation),
            "usage": dict(self.usage),
            "artifact_dir": self.artifact_dir,
            "created_at_utc": timestamp(self.created_at),
            "completed_at_utc": (
                None if self.completed_at is None else timestamp(self.completed_at)
            ),
        }


class ResearchRunStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return _connect(self.path)

    def create(
        self,
        *,
        session_id: str,
        prompt: str,
        model_config: Mapping[str, object],
        params: Mapping[str, object],
        status: str = "queued",
        parent_run_id: str | None = None,
        data_manifest_sha256: str | None = None,
        frozen_data_path: str | None = None,
        artifact_dir: str | None = None,
    ) -> ResearchRunRecord:
        if status not in RUN_STATUSES:
            raise ResearchLabError("Unknown research run status.")
        if data_manifest_sha256 is not None and len(data_manifest_sha256) != 64:
            raise ResearchLabError("A data manifest digest is 64 hex characters.")
        run_id = "run_" + secrets.token_urlsafe(18)
        now = self.clock.now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO research_runs (run_id, session_id, parent_run_id, "
                    "status, prompt, model_config_json, params_json, "
                    "data_manifest_sha256, frozen_data_path, metrics_json, "
                    "validation_json, usage_json, artifact_dir, created_at_utc, "
                    "completed_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        run_id,
                        session_id,
                        parent_run_id,
                        status,
                        _bounded(prompt, "Run prompt", MAX_PROMPT_CHARS),
                        _dumps(model_config),
                        _dumps(params),
                        data_manifest_sha256,
                        frozen_data_path,
                        "{}",
                        "{}",
                        "{}",
                        artifact_dir,
                        timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="research.run.created",
                    object_type="research_run",
                    object_id=run_id,
                    payload={
                        "session_id": session_id,
                        "parent_run_id": parent_run_id or "",
                    },
                )
        return self.get(run_id)

    def get(self, run_id: str) -> ResearchRunRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT run_id, session_id, parent_run_id, status, prompt, "
                "model_config_json, params_json, data_manifest_sha256, "
                "frozen_data_path, metrics_json, validation_json, usage_json, "
                "artifact_dir, created_at_utc, completed_at_utc "
                "FROM research_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ResearchLabError("Research run not found.")
        return ResearchRunRecord(
            run_id=str(row[0]),
            session_id=str(row[1]),
            parent_run_id=None if row[2] is None else str(row[2]),
            status=str(row[3]),
            prompt=str(row[4]),
            model_config=_mapping(row[5]),
            params=_mapping(row[6]),
            data_manifest_sha256=None if row[7] is None else str(row[7]),
            frozen_data_path=None if row[8] is None else str(row[8]),
            metrics=_mapping(row[9]),
            validation=_mapping(row[10]),
            usage=_mapping(row[11]),
            artifact_dir=None if row[12] is None else str(row[12]),
            created_at=parse_timestamp(str(row[13])),
            completed_at=None if row[14] is None else parse_timestamp(str(row[14])),
        )

    def list(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> tuple[ResearchRunRecord, ...]:
        if not 1 <= limit <= 200:
            raise ResearchLabError("Run limit must be between 1 and 200.")
        clauses: list[str] = []
        params: list[object] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status is not None:
            if status not in RUN_STATUSES:
                raise ResearchLabError("Unknown research run status.")
            clauses.append("status = ?")
            params.append(status)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT run_id FROM research_runs"
                + where
                + " ORDER BY created_at_utc DESC, run_id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return tuple(self.get(str(row[0])) for row in rows)

    def update_status(
        self,
        run_id: str,
        *,
        status: str,
        metrics: Mapping[str, object] | None = None,
        validation: Mapping[str, object] | None = None,
        usage: Mapping[str, object] | None = None,
        completed: bool = False,
    ) -> ResearchRunRecord:
        if status not in RUN_STATUSES:
            raise ResearchLabError("Unknown research run status.")
        now = self.clock.now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "UPDATE research_runs SET status = ?, "
                    "metrics_json = COALESCE(?, metrics_json), "
                    "validation_json = COALESCE(?, validation_json), "
                    "usage_json = COALESCE(?, usage_json), "
                    "completed_at_utc = CASE WHEN ? THEN ? ELSE completed_at_utc END "
                    "WHERE run_id = ?",
                    (
                        status,
                        None if metrics is None else _dumps(metrics),
                        None if validation is None else _dumps(validation),
                        None if usage is None else _dumps(usage),
                        1 if completed else 0,
                        timestamp(now),
                        run_id,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="research.run.status",
                    object_type="research_run",
                    object_id=run_id,
                    payload={"status": status},
                )
        return self.get(run_id)


@dataclass(frozen=True, slots=True)
class ResearchArtifactRecord:
    artifact_id: str
    slug: str
    revision: int
    parent_revision: int | None
    title: str
    body_md: str
    linked_run_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    created_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "slug": self.slug,
            "revision": self.revision,
            "parent_revision": self.parent_revision,
            "title": self.title,
            "body_md": self.body_md,
            "linked_run_ids": list(self.linked_run_ids),
            "evidence_refs": list(self.evidence_refs),
            "created_at_utc": timestamp(self.created_at),
        }


class ResearchArtifactStore:
    """Versioned artifacts with an optional FTS5 index and a LIKE fallback."""

    def __init__(
        self, path: Path, clock: Clock | None = None, *, fts: bool | None = None
    ) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self._fts: bool | None = None
        self._fts_setting = fts

    def _connect(self) -> sqlite3.Connection:
        return _connect(self.path)

    @property
    def fts_available(self) -> bool:
        self._ensure_fts()
        return bool(self._fts)

    def _ensure_fts(self) -> bool:
        if self._fts is not None:
            return self._fts
        if self._fts_setting is False:
            self._fts = False
            return False
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (FTS_TABLE,),
            ).fetchone()
            if existing is None:
                try:
                    connection.execute(
                        f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5("
                        "artifact_id UNINDEXED, title, body_md)"
                    )
                    connection.execute(
                        f"INSERT INTO {FTS_TABLE} (artifact_id, title, body_md) "
                        "SELECT artifact_id, title, body_md FROM research_artifacts"
                    )
                    # Artifacts are immutable, so insert is the only sync event.
                    connection.execute(
                        f"CREATE TRIGGER {FTS_TABLE}_insert AFTER INSERT ON "
                        "research_artifacts BEGIN "
                        f"INSERT INTO {FTS_TABLE} (artifact_id, title, body_md) "
                        "VALUES (new.artifact_id, new.title, new.body_md); END"
                    )
                    connection.commit()
                except sqlite3.OperationalError:
                    self._fts = False
                    return False
            self._fts = True
        return True

    def save(
        self,
        *,
        slug: str,
        title: str,
        body_md: str,
        linked_run_ids: Sequence[str] = (),
        evidence_refs: Sequence[str] = (),
    ) -> ResearchArtifactRecord:
        """Write the next revision of ``slug`` (revision 1 when it is new)."""
        clean_slug = _bounded(slug, "Artifact slug", MAX_TITLE_CHARS)
        artifact_id = "art_" + secrets.token_urlsafe(18)
        now = self.clock.now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                row = connection.execute(
                    "SELECT MAX(revision) FROM research_artifacts WHERE slug = ?",
                    (clean_slug,),
                ).fetchone()
                previous = None if row is None or row[0] is None else int(str(row[0]))
                revision = 1 if previous is None else previous + 1
                connection.execute(
                    "INSERT INTO research_artifacts (artifact_id, slug, revision, "
                    "parent_revision, title, body_md, linked_run_ids_json, "
                    "evidence_refs_json, created_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        artifact_id,
                        clean_slug,
                        revision,
                        previous,
                        _bounded(title, "Artifact title", MAX_TITLE_CHARS),
                        _bounded(body_md, "Artifact body", MAX_BODY_CHARS),
                        _dumps({"ids": list(linked_run_ids)}),
                        _dumps({"refs": list(evidence_refs)}),
                        timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="research.artifact.created",
                    object_type="research_artifact",
                    object_id=artifact_id,
                    payload={"slug": clean_slug, "revision": str(revision)},
                )
        return self.get(artifact_id)

    def _record(self, row: Sequence[object]) -> ResearchArtifactRecord:
        return ResearchArtifactRecord(
            artifact_id=str(row[0]),
            slug=str(row[1]),
            revision=int(str(row[2])),
            parent_revision=None if row[3] is None else int(str(row[3])),
            title=str(row[4]),
            body_md=str(row[5]),
            linked_run_ids=_lenient_strings(row[6], "ids"),
            evidence_refs=_lenient_strings(row[7], "refs"),
            created_at=parse_timestamp(str(row[8])),
        )

    _COLUMNS = (
        "artifact_id, slug, revision, parent_revision, title, body_md, "
        "linked_run_ids_json, evidence_refs_json, created_at_utc"
    )
    _QUALIFIED_COLUMNS = (
        "a.artifact_id, a.slug, a.revision, a.parent_revision, a.title, "
        "a.body_md, a.linked_run_ids_json, a.evidence_refs_json, a.created_at_utc"
    )

    def get(self, artifact_id: str) -> ResearchArtifactRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT {self._COLUMNS} FROM research_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ResearchLabError("Research artifact not found.")
        return self._record(row)

    def revisions(self, slug: str) -> tuple[ResearchArtifactRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {self._COLUMNS} FROM research_artifacts WHERE slug = ? "
                "ORDER BY revision ASC",
                (slug,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def latest(self, slug: str) -> ResearchArtifactRecord:
        revisions = self.revisions(slug)
        if not revisions:
            raise ResearchLabError("Research artifact not found.")
        return revisions[-1]

    def search(
        self, query: str, *, limit: int = 20
    ) -> tuple[ResearchArtifactRecord, ...]:
        if not 1 <= limit <= 100:
            raise ResearchLabError("Search limit must be between 1 and 100.")
        terms = _terms(query)
        if not terms:
            raise ResearchLabError("A search query needs at least one word.")
        if self._ensure_fts():
            try:
                return self._search_fts(terms, limit)
            except sqlite3.OperationalError:
                self._fts = False
        return self._search_like(terms, limit)

    def _search_fts(
        self, terms: list[str], limit: int
    ) -> tuple[ResearchArtifactRecord, ...]:
        match = " OR ".join(f'"{term}"' for term in terms)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {self._QUALIFIED_COLUMNS} "
                f"FROM {FTS_TABLE} JOIN research_artifacts AS a "
                f"ON a.artifact_id = {FTS_TABLE}.artifact_id "
                f"WHERE {FTS_TABLE} MATCH ? "
                f"ORDER BY bm25({FTS_TABLE}) ASC, a.created_at_utc DESC LIMIT ?",
                (match, limit),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def _search_like(
        self, terms: list[str], limit: int
    ) -> tuple[ResearchArtifactRecord, ...]:
        clauses = " OR ".join("(title LIKE ? OR body_md LIKE ?)" for _ in terms)
        params: list[object] = []
        for term in terms:
            params.extend([f"%{term}%", f"%{term}%"])
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {self._COLUMNS} FROM research_artifacts WHERE {clauses} "
                "ORDER BY created_at_utc DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def diff(self, slug: str, rev_a: int, rev_b: int) -> dict[str, object]:
        revisions = {item.revision: item for item in self.revisions(slug)}
        if rev_a not in revisions or rev_b not in revisions:
            raise ResearchLabError("Both revisions must exist for that slug.")
        left = revisions[rev_a]
        right = revisions[rev_b]
        unified = list(
            difflib.unified_diff(
                left.body_md.splitlines(),
                right.body_md.splitlines(),
                fromfile=f"{slug}@r{rev_a}",
                tofile=f"{slug}@r{rev_b}",
                lineterm="",
            )
        )
        return {
            "slug": slug,
            "left": left.to_dict(),
            "right": right.to_dict(),
            "title_changed": left.title != right.title,
            "unified": "\n".join(unified),
        }


def _lenient_strings(raw: object, key: str) -> tuple[str, ...]:
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, dict):
        return ()
    values = payload.get(key)
    if not isinstance(values, list):
        return ()
    return tuple(str(item) for item in values)


__all__ = [
    "FTS_TABLE",
    "MAX_BODY_CHARS",
    "MAX_PROMPT_CHARS",
    "MAX_TITLE_CHARS",
    "RUN_STATUSES",
    "ResearchArtifactRecord",
    "ResearchArtifactStore",
    "ResearchLabError",
    "ResearchRunRecord",
    "ResearchRunStore",
    "ResearchSessionRecord",
    "ResearchSessionStore",
]
