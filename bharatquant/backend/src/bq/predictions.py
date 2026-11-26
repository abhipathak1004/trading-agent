import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bq.core import (
    IDENTIFIER,
    Clock,
    SystemClock,
)
from bq.core import (
    canonical_digest as core_canonical_digest,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    parse_timestamp as core_parse_timestamp,
)
from bq.core import (
    utc as core_utc,
)
from bq.infra.audit import append_event


def _utc(value: datetime) -> datetime:
    try:
        return core_utc(value)
    except ValueError as exc:
        raise PredictionError("Prediction timestamps must be timezone-aware.") from exc


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return core_parse_timestamp(value)


class PredictionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredPrediction:
    prediction_id: str
    created_at: datetime
    window_start: datetime
    window_end: datetime
    probability: float
    positive_threshold: float | None
    prompt_hash: str | None
    manifest_hash: str | None
    prediction_hash: str


@dataclass(frozen=True, slots=True)
class StoredOutcome:
    prediction_id: str
    resolved_at: datetime
    occurred: bool
    evidence_source_id: str | None
    evidence_record_id: str | None
    outcome_hash: str


def _canonical_hash(payload: object) -> str:
    return core_canonical_digest(payload)


class PredictionStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=True)

    def freeze(
        self,
        *,
        prediction_id: str,
        window_start: datetime,
        window_end: datetime,
        probability: float,
        positive_threshold: float | None = None,
        prompt_hash: str | None = None,
        manifest_hash: str | None = None,
    ) -> StoredPrediction:
        if (
            not isinstance(prediction_id, str)
            or IDENTIFIER.fullmatch(prediction_id) is None
        ):
            raise PredictionError("Invalid prediction identifier.")
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not 0 <= probability <= 1
        ):
            raise PredictionError("Probability must be from 0 to 1.")
        if positive_threshold is not None and (
            isinstance(positive_threshold, bool)
            or not isinstance(positive_threshold, (int, float))
            or not 0 <= positive_threshold <= 1
        ):
            raise PredictionError("Positive threshold must be from 0 to 1.")

        start = _utc(window_start)
        end = _utc(window_end)
        if end <= start:
            raise PredictionError("Prediction window must end after it starts.")
        created_at = _utc(self.clock.now())
        if created_at >= start:
            raise PredictionError("Prediction must be frozen before the window starts.")

        payload = {
            "prediction_id": prediction_id,
            "created_at": _timestamp(created_at),
            "window_start": _timestamp(start),
            "window_end": _timestamp(end),
            "probability": probability,
            "positive_threshold": positive_threshold,
            "prompt_hash": prompt_hash,
            "manifest_hash": manifest_hash,
        }
        prediction_hash = _canonical_hash(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT prediction_hash FROM predictions WHERE prediction_id = ?",
                    (prediction_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != prediction_hash:
                        raise PredictionError(
                            "Prediction identity already has conflicting content."
                        )
                    return self.get(prediction_id)
                connection.execute(
                    "INSERT INTO predictions "
                    "(prediction_id, created_at, window_start, window_end, "
                    "probability, positive_threshold, prompt_hash, manifest_hash, "
                    "prediction_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        prediction_id,
                        _timestamp(created_at),
                        _timestamp(start),
                        _timestamp(end),
                        repr(probability),
                        repr(positive_threshold)
                        if positive_threshold is not None
                        else None,
                        prompt_hash,
                        manifest_hash,
                        prediction_hash,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=created_at,
                    actor="owner",
                    action="prediction.frozen",
                    object_type="prediction",
                    object_id=prediction_id,
                    payload={"prediction_hash": prediction_hash},
                )
        return self.get(prediction_id)

    def resolve(
        self,
        prediction_id: str,
        *,
        occurred: bool,
        evidence_source_id: str | None = None,
        evidence_record_id: str | None = None,
        resolved_at: datetime | None = None,
    ) -> StoredOutcome:
        if (
            not isinstance(prediction_id, str)
            or IDENTIFIER.fullmatch(prediction_id) is None
        ):
            raise PredictionError("Invalid prediction identifier.")
        if not isinstance(occurred, bool):
            raise PredictionError("Prediction outcome must be a boolean.")
        prediction = self.get(prediction_id)
        resolution_time = _utc(resolved_at or self.clock.now())
        if resolution_time < prediction.window_end:
            raise PredictionError(
                "Outcome cannot resolve before the prediction window ends."
            )
        payload = {
            "prediction_id": prediction_id,
            "resolved_at": _timestamp(resolution_time),
            "occurred": occurred,
            "evidence_source_id": evidence_source_id,
            "evidence_record_id": evidence_record_id,
        }
        outcome_hash = _canonical_hash(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT outcome_hash FROM prediction_outcomes "
                    "WHERE prediction_id = ?",
                    (prediction_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != outcome_hash:
                        raise PredictionError(
                            "Prediction already has conflicting resolution."
                        )
                    return self.get_outcome(prediction_id)
                connection.execute(
                    "INSERT INTO prediction_outcomes "
                    "(prediction_id, resolved_at, occurred, evidence_source_id, "
                    "evidence_record_id, outcome_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        prediction_id,
                        _timestamp(resolution_time),
                        1 if occurred else 0,
                        evidence_source_id,
                        evidence_record_id,
                        outcome_hash,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=resolution_time,
                    actor="owner",
                    action="prediction.resolved",
                    object_type="prediction",
                    object_id=prediction_id,
                    payload={"outcome_hash": outcome_hash},
                )
        return self.get_outcome(prediction_id)

    def get(self, prediction_id: str) -> StoredPrediction:
        if (
            not isinstance(prediction_id, str)
            or IDENTIFIER.fullmatch(prediction_id) is None
        ):
            raise PredictionError("Invalid prediction identifier.")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT prediction_id, created_at, window_start, window_end, "
                "probability, positive_threshold, prompt_hash, manifest_hash, "
                "prediction_hash FROM predictions WHERE prediction_id = ?",
                (prediction_id,),
            ).fetchone()
        if row is None:
            raise PredictionError("Prediction not found.")
        return StoredPrediction(
            prediction_id=str(row[0]),
            created_at=_parse_timestamp(str(row[1])),
            window_start=_parse_timestamp(str(row[2])),
            window_end=_parse_timestamp(str(row[3])),
            probability=float(row[4]),
            positive_threshold=float(row[5]) if row[5] is not None else None,
            prompt_hash=str(row[6]) if row[6] is not None else None,
            manifest_hash=str(row[7]) if row[7] is not None else None,
            prediction_hash=str(row[8]),
        )

    def get_outcome(self, prediction_id: str) -> StoredOutcome:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT prediction_id, resolved_at, occurred, evidence_source_id, "
                "evidence_record_id, outcome_hash "
                "FROM prediction_outcomes WHERE prediction_id = ?",
                (prediction_id,),
            ).fetchone()
        if row is None:
            raise PredictionError("Prediction outcome not found.")
        return StoredOutcome(
            prediction_id=str(row[0]),
            resolved_at=_parse_timestamp(str(row[1])),
            occurred=bool(row[2]),
            evidence_source_id=str(row[3]) if row[3] is not None else None,
            evidence_record_id=str(row[4]) if row[4] is not None else None,
            outcome_hash=str(row[5]),
        )
