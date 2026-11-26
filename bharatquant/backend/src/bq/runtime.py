"""Runtime health, metrics, backups and benchmarks.

These helpers deliberately return plain dictionaries so the CLI and API share one
implementation and the UI can render the same facts without a second contract.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bq.core import canonical_digest, parse_timestamp
from bq.infra.database import DatabaseError, check_database, packaged_migrations

MAX_BACKUP_BYTES = 50 * 1024**3
STALE_AFTER = timedelta(minutes=10)


class RuntimeError(ValueError):
    pass


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row is not None else 0


def _job_counts(connection: sqlite3.Connection) -> dict[str, int]:
    try:
        rows = connection.execute(
            "SELECT status, COUNT(*) FROM job_runs GROUP BY status"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row[0]): int(row[1]) for row in rows}


def inspect_runtime(
    path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    if instant.utcoffset() is None:
        raise RuntimeError("Runtime timestamp must be timezone-aware.")
    issues: list[str] = []
    if not path.is_file():
        return {
            "status": "degraded",
            "database": str(path),
            "issues": ["database_missing"],
            "checks": {},
        }
    try:
        check_database(path)
    except (DatabaseError, sqlite3.Error) as exc:
        issues.append(f"database: {exc}")
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    ) as connection:
        counts = _job_counts(connection)
        try:
            pending = int(counts.get("PENDING", 0))
            running = int(counts.get("RUNNING", 0))
            failed = int(counts.get("FAILED", 0))
            stale = int(
                connection.execute(
                    "SELECT COUNT(*) FROM job_runs "
                    "WHERE status = 'RUNNING' AND lease_until <= ?",
                    (
                        instant.isoformat(timespec="microseconds").replace(
                            "+00:00", "Z"
                        ),
                    ),
                ).fetchone()[0]
            )
            recent_failed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM job_run_events "
                    "WHERE event_type = 'FAILED' AND occurred_at >= ?",
                    (
                        (instant - timedelta(days=1))
                        .isoformat(timespec="microseconds")
                        .replace("+00:00", "Z"),
                    ),
                ).fetchone()[0]
            )
            last_event = connection.execute(
                "SELECT MAX(occurred_at) FROM job_run_events"
            ).fetchone()[0]
            reports = _table_count(connection, "reports")
            notes = _table_count(connection, "research_notes")
            paper_orders = _table_count(connection, "paper_orders")
            predictions = _table_count(connection, "predictions")
        except sqlite3.OperationalError as exc:
            issues.append(f"runtime query: {exc}")
            pending = running = failed = stale = recent_failed = 0
            last_event = None
            reports = notes = paper_orders = predictions = 0
    db_bytes = path.stat().st_size
    wal_path = path.with_name(path.name + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
    try:
        free_bytes = shutil.disk_usage(path.parent).free
    except OSError:
        free_bytes = 0
    if stale:
        issues.append(f"{stale} stale running job(s)")
    if wal_bytes > 512 * 1024**2:
        issues.append("WAL is larger than 512 MiB")
    if free_bytes and free_bytes < 1 * 1024**3:
        issues.append("less than 1 GiB disk free")
    return {
        "status": "ok" if not issues else "degraded",
        "database": str(path),
        "database_bytes": db_bytes,
        "wal_bytes": wal_bytes,
        "free_disk_bytes": free_bytes,
        "migration_count": len(packaged_migrations()),
        "integrity": "ok"
        if not any(item.startswith("database:") for item in issues)
        else "failed",
        "jobs": {
            "pending": pending,
            "running": running,
            "failed": failed,
            "stale": stale,
            "failed_24h": recent_failed,
            "last_event_at": last_event,
        },
        "counts": {
            "reports": reports,
            "research_notes": notes,
            "paper_orders": paper_orders,
            "predictions": predictions,
        },
        "issues": issues,
        "checked_at": instant.isoformat(),
    }


def metrics_text(status: dict[str, object]) -> str:
    """Small Prometheus-compatible text export without a new dependency."""
    jobs = status.get("jobs", {})
    counts = status.get("counts", {})
    if not isinstance(jobs, dict) or not isinstance(counts, dict):
        raise RuntimeError("Invalid runtime status.")
    lines = ["# HELP bq_runtime_degraded 1 when the runtime has open issues"]
    lines.append("# TYPE bq_runtime_degraded gauge")
    lines.append(
        f"bq_runtime_degraded {1 if status.get('status') == 'degraded' else 0}"
    )
    for name, value in (
        ("bq_database_bytes", status.get("database_bytes", 0)),
        ("bq_wal_bytes", status.get("wal_bytes", 0)),
        ("bq_free_disk_bytes", status.get("free_disk_bytes", 0)),
        ("bq_jobs_pending", jobs.get("pending", 0)),
        ("bq_jobs_running", jobs.get("running", 0)),
        ("bq_jobs_failed", jobs.get("failed", 0)),
        ("bq_jobs_stale", jobs.get("stale", 0)),
        ("bq_reports", counts.get("reports", 0)),
        ("bq_research_notes", counts.get("research_notes", 0)),
    ):
        lines.append(f"{name} {int(str(value))}")
    return "\n".join(lines) + "\n"


def backup_database(path: Path, destination: Path) -> dict[str, object]:
    if not path.is_file():
        raise RuntimeError("Database is not initialized.")
    if destination.resolve() == path.resolve():
        raise RuntimeError("Backup destination must differ from the database.")
    if destination.exists():
        raise RuntimeError("Backup destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(
            sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        ) as source,
        closing(sqlite3.connect(destination)) as target,
    ):
        source.backup(target)
    if destination.stat().st_size > MAX_BACKUP_BYTES:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Backup is larger than the allowed bound.")
    with closing(sqlite3.connect(destination)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            destination.unlink(missing_ok=True)
            raise RuntimeError("Backup integrity check failed.")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    manifest = {
        "source": str(path),
        "backup": str(destination),
        "created_at": datetime.now(UTC).isoformat(),
        "bytes": destination.stat().st_size,
        "sha256": digest,
        "migration_count": len(packaged_migrations()),
    }
    destination.with_suffix(destination.suffix + ".json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def restore_database(
    backup: Path, target: Path, *, confirm: bool = False
) -> dict[str, object]:
    if not confirm:
        raise RuntimeError("Restore requires explicit confirmation.")
    if not backup.is_file():
        raise RuntimeError("Backup not found.")
    with closing(sqlite3.connect(backup)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("Backup integrity check failed.")
        check_database(backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    pre_restore: str | None = None
    if target.exists():
        pre_restore_path = target.with_suffix(target.suffix + ".pre-restore")
        shutil.copy2(target, pre_restore_path)
        pre_restore = str(pre_restore_path)
    with (
        closing(sqlite3.connect(backup)) as source,
        closing(sqlite3.connect(target)) as destination,
    ):
        source.backup(destination)
    check_database(target)
    return {
        "target": str(target),
        "backup": str(backup),
        "pre_restore": pre_restore,
        "restored_at": datetime.now(UTC).isoformat(),
    }


def benchmark() -> dict[str, object]:
    """Cheap deterministic timings for the operational smoke check."""
    started = time.perf_counter()
    payload = {"symbol": "NSE:RELIANCE", "quantity": 10, "price": 100_00}
    for _ in range(10_000):
        canonical_digest(payload)
    digest_ms = (time.perf_counter() - started) * 1_000
    started = time.perf_counter()
    for _ in range(10_000):
        parse_timestamp("2026-11-26T10:00:00.000000Z")
    parse_ms = (time.perf_counter() - started) * 1_000
    return {
        "digest_10000_ms": round(digest_ms, 3),
        "parse_timestamp_10000_ms": round(parse_ms, 3),
        "status": "ok",
    }


__all__ = [
    "MAX_BACKUP_BYTES",
    "STALE_AFTER",
    "RuntimeError",
    "backup_database",
    "benchmark",
    "inspect_runtime",
    "metrics_text",
    "restore_database",
]
