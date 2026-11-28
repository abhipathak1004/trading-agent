import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from bq.context import BuiltContext, ContextError
from bq.core import (
    Clock,
    SystemClock,
)
from bq.core import (
    open_connection as core_open_connection,
)

HASH = re.compile(r"[0-9a-f]{64}")


class ContextManifestNotFound(LookupError):
    pass


def _validate(context: BuiltContext) -> datetime:
    if (
        not isinstance(context, BuiltContext)
        or not isinstance(context.manifest_hash, str)
        or HASH.fullmatch(context.manifest_hash) is None
    ):
        raise ContextError("Invalid context manifest hash.")
    try:
        encoded = context.manifest_json.encode("utf-8")
        manifest = json.loads(context.manifest_json)
        canonical = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        prompt_hash = sha256(context.prompt_json.encode("utf-8")).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ContextError("Invalid context manifest.") from exc
    if (
        not isinstance(manifest, dict)
        or canonical != context.manifest_json
        or sha256(encoded).hexdigest() != context.manifest_hash
        or manifest.get("prompt_json") != context.prompt_json
        or manifest.get("prompt_hash") != prompt_hash
    ):
        raise ContextError("Context manifest integrity check failed.")
    try:
        timestamp = manifest.get("as_of")
        if not isinstance(timestamp, str):
            raise ContextError("Context manifest is missing its as-of time.")
        as_of = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if as_of.utcoffset() is None:
            raise ContextError("Context manifest as-of time must be timezone-aware.")
        if (
            as_of.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
            != timestamp
        ):
            raise ContextError("Context manifest as-of time is not canonical.")
    except ValueError as exc:
        raise ContextError("Invalid context manifest as-of time.") from exc
    return as_of


class ContextManifestStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=False)

    def save(self, context: BuiltContext) -> None:
        as_of = _validate(context)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                row = connection.execute(
                    "SELECT manifest_json FROM context_manifests WHERE manifest_hash = ?",
                    (context.manifest_hash,),
                ).fetchone()
                if row is not None:
                    if row[0] != context.manifest_json:
                        raise sqlite3.DatabaseError("Context manifest hash conflict.")
                    return
                now = self.clock.now()
                if now.utcoffset() is None:
                    raise ContextError("Context clock must be timezone-aware.")
                if as_of > now:
                    raise ContextError("Context as-of time cannot be in the future.")
                recorded_at = (
                    now.astimezone(UTC)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
                connection.execute(
                    "INSERT INTO context_manifests VALUES (?, ?, ?)",
                    (context.manifest_hash, recorded_at, context.manifest_json),
                )

    def load(self, manifest_hash: str) -> BuiltContext:
        if not isinstance(manifest_hash, str) or HASH.fullmatch(manifest_hash) is None:
            raise ContextError("Invalid context manifest hash.")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT manifest_json FROM context_manifests WHERE manifest_hash = ?",
                (manifest_hash,),
            ).fetchone()
        if row is None:
            raise ContextManifestNotFound(
                f"Context manifest {manifest_hash} not found."
            )
        try:
            manifest = json.loads(row[0])
            if not isinstance(manifest, dict) or not isinstance(
                manifest.get("prompt_json"), str
            ):
                raise ContextError("Invalid stored context manifest.")
            context = BuiltContext(manifest["prompt_json"], row[0], manifest_hash)
            _validate(context)
        except (ContextError, ValueError, TypeError) as exc:
            raise sqlite3.DatabaseError(
                "Context manifest integrity check failed."
            ) from exc
        return context
