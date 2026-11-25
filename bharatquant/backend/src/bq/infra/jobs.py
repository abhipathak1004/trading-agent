import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from bq.core import Clock, SystemClock

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
OPAQUE_IDENTIFIER = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._:-]{0,127}")
FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class JobRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class JobRunError(RuntimeError):
    pass


class JobRunConflict(JobRunError):
    pass


@dataclass(frozen=True, slots=True)
class JobRun:
    run_id: str
    job_id: str
    occurrence_key: str
    scheduled_at: datetime
    available_at: datetime
    status: JobRunStatus
    attempts: int
    max_attempts: int
    lease_owner: str | None
    lease_token: str | None
    lease_until: datetime | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class JobRunEvent:
    sequence: int
    event_type: str
    occurred_at: datetime
    attempt: int
    worker_id: str | None
    failure_code: str | None


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Job timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _required_timestamp(value: str | None) -> datetime:
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise sqlite3.DatabaseError("Job run is missing a required timestamp.")
    return parsed


def _validate_identifier(value: str, name: str) -> None:
    if IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"Invalid {name}.")


def _validate_opaque_identifier(value: str, name: str) -> None:
    if OPAQUE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"Invalid {name}.")


class JobRunStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def enqueue(
        self,
        job_id: str,
        occurrence_key: str,
        *,
        scheduled_at: datetime,
        max_attempts: int,
    ) -> JobRun:
        _validate_identifier(job_id, "job identifier")
        _validate_identifier(occurrence_key, "occurrence key")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("Maximum attempts must be an integer.")
        if max_attempts < 1:
            raise ValueError("Maximum attempts must be positive.")
        scheduled_text = _timestamp(scheduled_at)
        run_id = secrets.token_urlsafe(18)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                now = _utc(self.clock.now())
                existing = connection.execute(
                    "SELECT run_id, scheduled_at, max_attempts FROM job_runs "
                    "WHERE job_id = ? AND occurrence_key = ?",
                    (job_id, occurrence_key),
                ).fetchone()
                if existing is not None:
                    if existing[1] != scheduled_text or existing[2] != max_attempts:
                        raise JobRunConflict(
                            "Occurrence already exists with different parameters."
                        )
                    return self._get(connection, str(existing[0]))

                connection.execute(
                    "INSERT INTO job_runs "
                    "(run_id, job_id, occurrence_key, scheduled_at, available_at, "
                    "status, attempts, max_attempts) "
                    "VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?)",
                    (
                        run_id,
                        job_id,
                        occurrence_key,
                        scheduled_text,
                        scheduled_text,
                        max_attempts,
                    ),
                )
                self._append_event(
                    connection,
                    run_id,
                    "ENQUEUED",
                    now,
                    0,
                    None,
                    None,
                )
                return self._get(connection, run_id)

    def claim_next(self, worker_id: str, *, lease_for: timedelta) -> JobRun | None:
        _validate_identifier(worker_id, "worker identifier")
        if lease_for <= timedelta(0):
            raise ValueError("Job lease duration must be positive.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                now = _utc(self.clock.now())
                now_text = _timestamp(now)
                lease_until = _timestamp(now + lease_for)
                expired = connection.execute(
                    "SELECT run_id, attempts FROM job_runs "
                    "WHERE status = 'RUNNING' AND lease_until <= ? "
                    "AND attempts >= max_attempts",
                    (now_text,),
                ).fetchall()
                for run_id, attempts in expired:
                    connection.execute(
                        "UPDATE job_runs SET status = 'FAILED', lease_owner = NULL, "
                        "lease_token = NULL, lease_until = NULL, "
                        "failure_code = 'lease_expired' "
                        "WHERE run_id = ?",
                        (run_id,),
                    )
                    self._append_event(
                        connection,
                        str(run_id),
                        "LEASE_EXPIRED",
                        now,
                        int(attempts),
                        None,
                        "lease_expired",
                    )

                row = connection.execute(
                    "SELECT run_id, status, attempts FROM job_runs "
                    "WHERE (status = 'PENDING' AND available_at <= ?) "
                    "OR (status = 'RUNNING' AND lease_until <= ? "
                    "AND attempts < max_attempts) "
                    "ORDER BY available_at, scheduled_at, run_id LIMIT 1",
                    (now_text, now_text),
                ).fetchone()
                if row is None:
                    return None

                run_id, previous_status, attempts = row
                attempt = int(attempts) + 1
                lease_token = secrets.token_urlsafe(24)
                connection.execute(
                    "UPDATE job_runs SET status = 'RUNNING', attempts = ?, "
                    "lease_owner = ?, lease_token = ?, lease_until = ? "
                    "WHERE run_id = ?",
                    (attempt, worker_id, lease_token, lease_until, run_id),
                )
                self._append_event(
                    connection,
                    str(run_id),
                    (
                        "LEASE_RECLAIMED"
                        if previous_status == JobRunStatus.RUNNING.value
                        else "CLAIMED"
                    ),
                    now,
                    attempt,
                    worker_id,
                    None,
                )
                return self._get(connection, str(run_id))

    def succeed(self, run_id: str, worker_id: str, *, lease_token: str) -> JobRun:
        return self._finish(
            run_id,
            worker_id,
            lease_token=lease_token,
            succeeded=True,
            failure_code=None,
        )

    def fail(
        self, run_id: str, worker_id: str, failure_code: str, *, lease_token: str
    ) -> JobRun:
        if FAILURE_CODE.fullmatch(failure_code) is None:
            raise ValueError("Invalid job failure code.")
        return self._finish(
            run_id,
            worker_id,
            lease_token=lease_token,
            succeeded=False,
            failure_code=failure_code,
        )

    def _finish(
        self,
        run_id: str,
        worker_id: str,
        *,
        lease_token: str,
        succeeded: bool,
        failure_code: str | None,
    ) -> JobRun:
        _validate_opaque_identifier(run_id, "run identifier")
        _validate_identifier(worker_id, "worker identifier")
        _validate_opaque_identifier(lease_token, "lease token")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                now = _utc(self.clock.now())
                now_text = _timestamp(now)
                current = self._get(connection, run_id)
                if (
                    current.status is not JobRunStatus.RUNNING
                    or current.lease_owner != worker_id
                    or current.lease_token != lease_token
                    or current.lease_until is None
                    or current.lease_until <= now
                ):
                    raise JobRunConflict("Worker does not hold an active job lease.")
                if succeeded:
                    status = JobRunStatus.SUCCEEDED
                    available_at = _timestamp(current.available_at)
                    event_type = "SUCCEEDED"
                    stored_failure = None
                elif current.attempts < current.max_attempts:
                    status = JobRunStatus.PENDING
                    available_at = now_text
                    event_type = "RETRY_SCHEDULED"
                    stored_failure = failure_code
                else:
                    status = JobRunStatus.FAILED
                    available_at = _timestamp(current.available_at)
                    event_type = "FAILED"
                    stored_failure = failure_code
                connection.execute(
                    "UPDATE job_runs SET status = ?, available_at = ?, "
                    "lease_owner = NULL, lease_token = NULL, lease_until = NULL, "
                    "failure_code = ? "
                    "WHERE run_id = ?",
                    (status.value, available_at, stored_failure, run_id),
                )
                self._append_event(
                    connection,
                    run_id,
                    event_type,
                    now,
                    current.attempts,
                    worker_id,
                    failure_code,
                )
                return self._get(connection, run_id)

    def get(self, run_id: str) -> JobRun:
        _validate_opaque_identifier(run_id, "run identifier")
        with closing(self._connect()) as connection:
            return self._get(connection, run_id)

    def find(self, job_id: str, occurrence_key: str) -> JobRun | None:
        """Return an existing occurrence, or ``None`` if it is not enqueued."""
        _validate_identifier(job_id, "job identifier")
        _validate_identifier(occurrence_key, "occurrence key")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT run_id FROM job_runs WHERE job_id = ? AND occurrence_key = ?",
                (job_id, occurrence_key),
            ).fetchone()
            if row is None:
                return None
            return self._get(connection, str(row[0]))

    def events_for(self, run_id: str) -> tuple[JobRunEvent, ...]:
        _validate_opaque_identifier(run_id, "run identifier")
        with closing(self._connect()) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM job_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                is None
            ):
                raise JobRunError(f"Job run {run_id} does not exist.")
            rows = connection.execute(
                "SELECT sequence, event_type, occurred_at, attempt, worker_id, "
                "failure_code FROM job_run_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return tuple(
            JobRunEvent(
                sequence=int(row[0]),
                event_type=str(row[1]),
                occurred_at=_required_timestamp(str(row[2])),
                attempt=int(row[3]),
                worker_id=str(row[4]) if row[4] is not None else None,
                failure_code=str(row[5]) if row[5] is not None else None,
            )
            for row in rows
        )

    @staticmethod
    def _get(connection: sqlite3.Connection, run_id: str) -> JobRun:
        row = connection.execute(
            "SELECT run_id, job_id, occurrence_key, scheduled_at, available_at, "
            "status, attempts, max_attempts, lease_owner, lease_token, lease_until, "
            "failure_code "
            "FROM job_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise JobRunError(f"Job run {run_id} does not exist.")
        return JobRun(
            run_id=str(row[0]),
            job_id=str(row[1]),
            occurrence_key=str(row[2]),
            scheduled_at=_required_timestamp(str(row[3])),
            available_at=_required_timestamp(str(row[4])),
            status=JobRunStatus(str(row[5])),
            attempts=int(row[6]),
            max_attempts=int(row[7]),
            lease_owner=str(row[8]) if row[8] is not None else None,
            lease_token=str(row[9]) if row[9] is not None else None,
            lease_until=_parse_timestamp(row[10]),
            failure_code=str(row[11]) if row[11] is not None else None,
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        occurred_at: datetime,
        attempt: int,
        worker_id: str | None,
        failure_code: str | None,
    ) -> None:
        connection.execute(
            "INSERT INTO job_run_events "
            "(run_id, event_type, occurred_at, attempt, worker_id, failure_code) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                event_type,
                _timestamp(occurred_at),
                attempt,
                worker_id,
                failure_code,
            ),
        )
