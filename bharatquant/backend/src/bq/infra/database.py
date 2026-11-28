import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from bq.infra.audit import AuditIntegrityError, verify_chain

APPLICATION_ID = 0x42515400
MIGRATION_NAME = re.compile(r"([0-9]{3})_[a-z0-9_]+\.sql")


class DatabaseError(Exception):
    """The requested operation is unsafe for the current database state."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: bytes

    @property
    def checksum(self) -> str:
        return sha256(self.sql).hexdigest()


def packaged_migrations() -> list[Migration]:
    directory = files("bq.infra").joinpath("migrations")
    migrations = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        match = MIGRATION_NAME.fullmatch(entry.name)
        if not match or not entry.is_file():
            raise DatabaseError(f"Unexpected migration resource: {entry.name}")
        migrations.append(Migration(int(match[1]), entry.name, entry.read_bytes()))
    if not migrations or [item.version for item in migrations] != list(
        range(1, len(migrations) + 1)
    ):
        raise DatabaseError("Migration files must be consecutively numbered from 001.")
    return migrations


def _statements(text: str) -> Iterator[str]:
    start = 0
    for index, char in enumerate(text):
        if char == ";" and sqlite3.complete_statement(text[start : index + 1]):
            yield text[start : index + 1]
            start = index + 1
    if text[start:].strip():
        raise DatabaseError("Migration contains an unfinished SQL statement.")


def _applied_count(connection: sqlite3.Connection, migrations: list[Migration]) -> int:
    applied = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    expected = [
        (item.version, item.name, item.checksum) for item in migrations[: len(applied)]
    ]
    if applied != expected:
        index = 0
        while (
            index < min(len(applied), len(expected))
            and applied[index] == expected[index]
        ):
            index += 1
        source = applied if len(applied) > len(expected) else expected
        version, name = source[index][:2]
        raise DatabaseError(
            "Migration history differs from the packaged migrations at "
            f"{version:03d} {name}."
        )
    return len(applied)


def migrate(path: Path, *, create: bool = False) -> int:
    migrations = packaged_migrations()
    mode = "rwc" if create else "rw"
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True)
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            if application_id == 0 and create:
                if connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                    raise DatabaseError(
                        "Refusing to initialize a non-BharatQuant database."
                    )
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID:d}")
                connection.execute(
                    """CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        checksum TEXT NOT NULL
                    ) STRICT"""
                )
            elif application_id != APPLICATION_ID:
                raise DatabaseError("Not a BharatQuant database.")

            applied_count = _applied_count(connection, migrations)
            for migration in migrations[applied_count:]:
                for statement in _statements(migration.sql.decode("utf-8")):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                    (migration.version, migration.name, migration.checksum),
                )

        if connection.execute("PRAGMA journal_mode = WAL").fetchone() != ("wal",):
            raise DatabaseError("Unable to activate SQLite WAL mode.")
    return len(migrations) - applied_count


def check_database(path: Path) -> None:
    if not path.is_file():
        raise DatabaseError("Database is not initialized. Run 'bq init' first.")
    migrations = packaged_migrations()
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    ) as connection:
        if connection.execute("PRAGMA application_id").fetchone() != (APPLICATION_ID,):
            raise DatabaseError("Not a BharatQuant database.")
        if _applied_count(connection, migrations) != len(migrations):
            raise DatabaseError("Database needs migration. Run 'bq db migrate'.")
        if connection.execute("PRAGMA journal_mode").fetchone() != ("wal",):
            raise DatabaseError("SQLite WAL inactive. Run 'bq db migrate'.")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise DatabaseError("Database integrity check failed.")
        try:
            verify_chain(connection)
        except AuditIntegrityError as exc:
            raise DatabaseError(str(exc)) from exc
