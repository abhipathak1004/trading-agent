import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from bq.core import Clock, SystemClock
from bq.infra.contexts import ContextManifestStore

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_INTEGER = 9223372036854775807


class AttemptError(ValueError):
    pass


class AttemptConflict(AttemptError):
    pass


class AttemptNotFound(LookupError):
    pass


class AttemptStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    INTERRUPTED = "INTERRUPTED"
    CACHE_HIT = "CACHE_HIT"


class UsageKind(StrEnum):
    REPORTED = "REPORTED"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"
    NO_CALL = "NO_CALL"


def _identifier(value: str) -> None:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise AttemptError("Invalid attempt identifier.")


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise AttemptError("Attempt clock must be timezone-aware.")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    status: AttemptStatus
    usage_kind: UsageKind
    input_tokens: int | None
    output_tokens: int | None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AttemptStatus) or not isinstance(
            self.usage_kind, UsageKind
        ):
            raise AttemptError("Invalid attempt outcome or usage kind.")
        for value in (self.input_tokens, self.output_tokens):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_INTEGER
            ):
                raise AttemptError("Token counts must be nonnegative integers.")
        if self.usage_kind is UsageKind.UNKNOWN:
            if self.input_tokens is not None or self.output_tokens is not None:
                raise AttemptError("Unknown usage cannot include token counts.")
        elif self.usage_kind is UsageKind.NO_CALL:
            if (
                self.status is not AttemptStatus.CACHE_HIT
                or self.input_tokens != 0
                or self.output_tokens != 0
            ):
                raise AttemptError(
                    "No-call usage is only valid for a zero-token cache hit."
                )
        elif self.input_tokens is None and self.output_tokens is None:
            raise AttemptError("Reported or estimated usage must include a count.")
        if (
            self.status is AttemptStatus.CACHE_HIT
            and self.usage_kind is not UsageKind.NO_CALL
        ):
            raise AttemptError("A cache hit must record no provider call.")
        if self.status in (AttemptStatus.SUCCEEDED, AttemptStatus.CACHE_HIT):
            if self.failure_code is not None:
                raise AttemptError("Successful attempts cannot have a failure code.")
        elif (
            not isinstance(self.failure_code, str)
            or FAILURE_CODE.fullmatch(self.failure_code) is None
        ):
            raise AttemptError("Failed attempts require a valid failure code.")


@dataclass(frozen=True, slots=True)
class ModelAttempt:
    attempt_id: str
    task_id: str
    task_version: str
    model_id: str
    manifest_hash: str
    started_at: datetime
    finished_at: datetime | None
    outcome: AttemptOutcome | None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    reported_tokens: int
    reported_count: int
    estimated_tokens: int
    estimated_count: int
    unknown_count: int


@dataclass(frozen=True, slots=True)
class ModelUsageSummary:
    task_id: str
    task_version: str
    model_id: str
    attempt_count: int
    incomplete_count: int
    cache_hits: int
    input: TokenUsage
    output: TokenUsage


@dataclass(slots=True)
class _TokenCounter:
    reported_tokens: int = 0
    reported_count: int = 0
    estimated_tokens: int = 0
    estimated_count: int = 0
    unknown_count: int = 0

    def add(self, kind: UsageKind | None, tokens: int | None) -> None:
        if kind is UsageKind.NO_CALL:
            return
        if tokens is None:
            self.unknown_count += 1
        elif kind is UsageKind.REPORTED:
            self.reported_count += 1
            self.reported_tokens += tokens
        elif kind is UsageKind.ESTIMATED:
            self.estimated_count += 1
            self.estimated_tokens += tokens
        else:
            raise sqlite3.DatabaseError("Invalid token usage classification.")

    def freeze(self) -> TokenUsage:
        return TokenUsage(
            self.reported_tokens,
            self.reported_count,
            self.estimated_tokens,
            self.estimated_count,
            self.unknown_count,
        )


@dataclass(slots=True)
class _UsageCounter:
    attempt_count: int
    incomplete_count: int
    cache_hits: int
    input: _TokenCounter
    output: _TokenCounter


class ModelAttemptStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def start(
        self,
        attempt_id: str,
        *,
        task_id: str,
        task_version: str,
        model_id: str,
        manifest_hash: str,
    ) -> ModelAttempt:
        for value in (attempt_id, task_id, task_version, model_id):
            _identifier(value)
        context = ContextManifestStore(self.path, self.clock).load(manifest_hash)
        manifest = json.loads(context.manifest_json)
        if manifest.get("model_id") != model_id:
            raise AttemptError("Attempt model does not match the context manifest.")
        if (
            manifest.get("task_id") != task_id
            or manifest.get("task_version") != task_version
        ):
            raise AttemptError("Attempt task does not match the context manifest.")
        as_of = datetime.fromisoformat(manifest["as_of"].replace("Z", "+00:00"))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                if connection.execute(
                    "SELECT 1 FROM model_attempts WHERE attempt_id = ?", (attempt_id,)
                ).fetchone():
                    raise AttemptConflict("Attempt identity has already been used.")
                now = self.clock.now()
                now_text = _timestamp(now)
                if as_of > now:
                    raise AttemptError(
                        "Attempt cannot start before the context as-of time."
                    )
                connection.execute(
                    "INSERT INTO model_attempts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        attempt_id,
                        task_id,
                        task_version,
                        model_id,
                        manifest_hash,
                        now_text,
                    ),
                )
                return self._get(connection, attempt_id)

    def finish(self, attempt_id: str, outcome: AttemptOutcome) -> ModelAttempt:
        _identifier(attempt_id)
        if not isinstance(outcome, AttemptOutcome):
            raise AttemptError("Attempt outcome is required.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                current = self._get(connection, attempt_id)
                if current.outcome is not None:
                    raise AttemptConflict("Attempt already has an outcome.")
                now = self.clock.now()
                now_text = _timestamp(now)
                if now < current.started_at:
                    raise AttemptError("Attempt clock moved before the start time.")
                connection.execute(
                    "INSERT INTO model_attempt_outcomes VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        attempt_id,
                        now_text,
                        outcome.status.value,
                        outcome.usage_kind.value,
                        outcome.input_tokens,
                        outcome.output_tokens,
                        outcome.failure_code,
                    ),
                )
                return self._get(connection, attempt_id)

    def get(self, attempt_id: str) -> ModelAttempt:
        _identifier(attempt_id)
        with closing(self._connect()) as connection:
            return self._get(connection, attempt_id)

    def incomplete(self, *, limit: int = 100) -> tuple[ModelAttempt, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise AttemptError("Incomplete attempt limit must be between 1 and 1000.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT a.attempt_id FROM model_attempts a LEFT JOIN model_attempt_outcomes o "
                "ON a.attempt_id = o.attempt_id WHERE o.attempt_id IS NULL "
                "ORDER BY a.started_at, a.attempt_id LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(self._get(connection, str(row[0])) for row in rows)

    def usage(self) -> tuple[ModelUsageSummary, ...]:
        counters: dict[tuple[str, str, str], _UsageCounter] = {}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT a.task_id, a.task_version, a.model_id, o.status, "
                "o.usage_kind, o.input_tokens, o.output_tokens, o.failure_code "
                "FROM model_attempts a LEFT JOIN model_attempt_outcomes o "
                "ON a.attempt_id = o.attempt_id"
            )
            for (
                task_id,
                task_version,
                model_id,
                status,
                usage_kind,
                input_tokens,
                output_tokens,
                failure_code,
            ) in rows:
                try:
                    for value in (task_id, task_version, model_id):
                        _identifier(value)
                    outcome = (
                        AttemptOutcome(
                            AttemptStatus(status),
                            UsageKind(usage_kind),
                            input_tokens,
                            output_tokens,
                            failure_code,
                        )
                        if status is not None
                        else None
                    )
                except (TypeError, ValueError) as exc:
                    raise sqlite3.DatabaseError("Invalid stored model usage.") from exc
                key = (task_id, task_version, model_id)
                if key not in counters:
                    counters[key] = _UsageCounter(
                        0, 0, 0, _TokenCounter(), _TokenCounter()
                    )
                counter = counters[key]
                counter.attempt_count += 1
                if outcome is None:
                    counter.incomplete_count += 1
                elif outcome.usage_kind is UsageKind.NO_CALL:
                    counter.cache_hits += 1
                kind = outcome.usage_kind if outcome is not None else None
                counter.input.add(
                    kind, outcome.input_tokens if outcome is not None else None
                )
                counter.output.add(
                    kind, outcome.output_tokens if outcome is not None else None
                )
        return tuple(
            ModelUsageSummary(
                task_id,
                task_version,
                model_id,
                counter.attempt_count,
                counter.incomplete_count,
                counter.cache_hits,
                counter.input.freeze(),
                counter.output.freeze(),
            )
            for (task_id, task_version, model_id), counter in sorted(counters.items())
        )

    @staticmethod
    def _get(connection: sqlite3.Connection, attempt_id: str) -> ModelAttempt:
        row = connection.execute(
            "SELECT a.attempt_id, a.task_id, a.task_version, a.model_id, a.manifest_hash, "
            "a.started_at, o.finished_at, o.status, o.usage_kind, o.input_tokens, "
            "o.output_tokens, o.failure_code FROM model_attempts a "
            "LEFT JOIN model_attempt_outcomes o ON a.attempt_id = o.attempt_id "
            "WHERE a.attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise AttemptNotFound(f"Model attempt {attempt_id} not found.")
        try:
            started_at = datetime.fromisoformat(row[5].replace("Z", "+00:00"))
            finished_at = (
                datetime.fromisoformat(row[6].replace("Z", "+00:00"))
                if row[6] is not None
                else None
            )
            if _timestamp(started_at) != row[5] or (
                finished_at is not None
                and (_timestamp(finished_at) != row[6] or finished_at < started_at)
            ):
                raise AttemptError("Invalid attempt timestamps.")
            outcome = (
                AttemptOutcome(
                    AttemptStatus(row[7]), UsageKind(row[8]), row[9], row[10], row[11]
                )
                if row[6] is not None
                else None
            )
            return ModelAttempt(
                attempt_id=row[0],
                task_id=row[1],
                task_version=row[2],
                model_id=row[3],
                manifest_hash=row[4],
                started_at=started_at,
                finished_at=finished_at,
                outcome=outcome,
            )
        except (ValueError, TypeError) as exc:
            raise sqlite3.DatabaseError("Invalid stored model attempt.") from exc
