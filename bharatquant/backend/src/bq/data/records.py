import json
import re
import secrets
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from bq.core import Clock, SystemClock
from bq.infra.audit import append_event

RECORD_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


class RecordKind(StrEnum):
    INSTRUMENT = "INSTRUMENT"
    EOD_BAR = "EOD_BAR"
    SNAPSHOT = "SNAPSHOT"
    FILING = "FILING"
    MARKET_STATISTIC = "MARKET_STATISTIC"


class QualityStatus(StrEnum):
    UNASSESSED = "UNASSESSED"
    ACCEPTABLE = "ACCEPTABLE"
    DEGRADED = "DEGRADED"
    REJECTED = "REJECTED"


class MissingDataRecord(LookupError):
    pass


class DataImportConflict(ValueError):
    pass


class MissingDataImport(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class DataImportResult:
    import_id: str
    record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DataImportInfo:
    import_id: str
    source_id: str
    recorded_at: datetime
    record_count: int


@dataclass(frozen=True, slots=True)
class DataImportSummary:
    import_count: int
    record_count: int
    recent_imports: tuple[DataImportInfo, ...]


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Data record timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _canonical_payload(payload: Mapping[str, object]) -> str:
    try:
        serialized = json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Data record payload must be finite JSON data.") from exc
    if not isinstance(json.loads(serialized), dict):
        raise ValueError("Data record payload must be a JSON object.")
    return serialized


@dataclass(frozen=True, slots=True)
class DataRecord:
    kind: RecordKind
    record_key: str
    source_id: str
    source_url: str
    unit: str | None
    event_at: datetime | None
    published_at: datetime | None
    known_at: datetime
    received_at: datetime
    quality_status: QualityStatus
    payload_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RecordKind):
            raise TypeError("Invalid data record kind.")
        if RECORD_KEY.fullmatch(self.record_key) is None:
            raise ValueError("Invalid data record key.")
        if RECORD_KEY.fullmatch(self.source_id) is None:
            raise ValueError("Invalid data source identifier.")
        source = urlsplit(self.source_url)
        if (
            source.scheme != "https"
            or not source.hostname
            or source.username is not None
            or source.password is not None
        ):
            raise ValueError("Data source must be an HTTPS URL without credentials.")
        if self.unit is not None and not self.unit.strip():
            raise ValueError("Data record unit cannot be empty.")
        if not isinstance(self.quality_status, QualityStatus):
            raise TypeError("Invalid data quality status.")

        known_at = _utc(self.known_at)
        received_at = _utc(self.received_at)
        if received_at > known_at:
            raise ValueError("Data cannot be known before it is received.")
        if self.event_at is not None:
            _utc(self.event_at)
        if self.published_at is not None and _utc(self.published_at) > known_at:
            raise ValueError("Data cannot be known before its publication time.")

        try:
            canonical_payload = _canonical_payload(json.loads(self.payload_json))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(
                "Data record payload must be a canonical JSON object."
            ) from exc
        if canonical_payload != self.payload_json:
            raise ValueError("Data record payload must use canonical JSON encoding.")

    @classmethod
    def create(
        cls,
        *,
        kind: RecordKind,
        record_key: str,
        source_id: str,
        source_url: str,
        unit: str | None = None,
        event_at: datetime | None = None,
        published_at: datetime | None = None,
        known_at: datetime,
        received_at: datetime,
        quality_status: QualityStatus = QualityStatus.UNASSESSED,
        payload: Mapping[str, object],
    ) -> "DataRecord":
        return cls(
            kind=kind,
            record_key=record_key,
            source_id=source_id,
            source_url=source_url,
            unit=unit,
            event_at=event_at,
            published_at=published_at,
            known_at=known_at,
            received_at=received_at,
            quality_status=quality_status,
            payload_json=_canonical_payload(payload),
        )

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise ValueError("Data record payload must be a JSON object.")
        return value


@dataclass(frozen=True, slots=True)
class DataRecordVersion:
    record_id: str
    recorded_at: datetime
    record: DataRecord


def _decode_record(row: tuple[object, ...]) -> DataRecord:
    payload_json = str(row[10])
    actual_hash = sha256(payload_json.encode("utf-8")).hexdigest()
    if actual_hash != row[11]:
        raise sqlite3.DatabaseError(
            f"Data record payload integrity check failed for {row[1]}."
        )
    known_at = _parse_timestamp(str(row[7]))
    received_at = _parse_timestamp(str(row[8]))
    if known_at is None or received_at is None:
        raise sqlite3.DatabaseError("Data record is missing availability timestamps.")
    return DataRecord(
        kind=RecordKind(str(row[0])),
        record_key=str(row[1]),
        source_id=str(row[2]),
        source_url=str(row[3]),
        unit=str(row[4]) if row[4] is not None else None,
        event_at=_parse_timestamp(str(row[5]) if row[5] is not None else None),
        published_at=_parse_timestamp(str(row[6]) if row[6] is not None else None),
        known_at=known_at,
        received_at=received_at,
        quality_status=QualityStatus(str(row[9])),
        payload_json=payload_json,
    )


def _decode_version(record_id: str, row: tuple[object, ...]) -> DataRecordVersion:
    try:
        recorded_at = _parse_timestamp(str(row[12]))
    except ValueError as exc:
        raise sqlite3.DatabaseError("Data record has an invalid record time.") from exc
    if recorded_at is None or recorded_at.utcoffset() is None:
        raise sqlite3.DatabaseError("Data record has an invalid record time.")
    return DataRecordVersion(record_id, recorded_at, _decode_record(row))


class DataRecordStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def record(self, record: DataRecord) -> str:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                now = _utc(self.clock.now())
                return self._insert_record(connection, record, now)

    def import_summary(self) -> DataImportSummary:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(record_count), 0) FROM data_imports"
            ).fetchone()
            rows = connection.execute(
                "SELECT import_id, source_id, recorded_at, record_count FROM data_imports "
                "ORDER BY recorded_at DESC, import_id DESC LIMIT 20"
            ).fetchall()
        recent = []
        for import_id, source_id, recorded_at, record_count in rows:
            try:
                instant = _parse_timestamp(recorded_at)
            except ValueError as exc:
                raise sqlite3.DatabaseError(
                    "Data import has an invalid timestamp."
                ) from exc
            if instant is None or instant.utcoffset() is None:
                raise sqlite3.DatabaseError("Data import has an invalid timestamp.")
            recent.append(
                DataImportInfo(
                    str(import_id), str(source_id), instant, int(record_count)
                )
            )
        return DataImportSummary(
            import_count=int(row[0]),
            record_count=int(row[1]),
            recent_imports=tuple(recent),
        )

    def import_records_for_keys(
        self,
        import_id: str,
        *,
        source_id: str,
        record_keys: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[str, ...]:
        if (
            not isinstance(import_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", import_id) is None
        ):
            raise ValueError("Invalid data import identifier.")
        if not isinstance(source_id, str) or RECORD_KEY.fullmatch(source_id) is None:
            raise ValueError("Invalid data source identifier.")
        if (
            not isinstance(record_keys, tuple)
            or not 1 <= len(record_keys) <= 366
            or any(
                not isinstance(key, str) or RECORD_KEY.fullmatch(key) is None
                for key in record_keys
            )
            or len(set(record_keys)) != len(record_keys)
        ):
            raise ValueError("Expected 1 to 366 distinct record keys.")
        as_of_text = _timestamp(as_of)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            info = connection.execute(
                "SELECT source_id, record_count FROM data_imports "
                "WHERE import_id = ? AND recorded_at <= ?",
                (import_id, as_of_text),
            ).fetchone()
            if info is None:
                raise MissingDataImport(
                    "Data import is unavailable at the requested time."
                )
            if info[0] != source_id:
                raise ValueError(
                    "Data import source does not match the requested source."
                )
            count, minimum, maximum, linked = connection.execute(
                "SELECT COUNT(*), MIN(m.ordinal), MAX(m.ordinal), COUNT(r.record_id) "
                "FROM data_import_records AS m "
                "LEFT JOIN point_in_time_records AS r ON r.record_id = m.record_id "
                "WHERE m.import_id = ?",
                (import_id,),
            ).fetchone()
            if (
                count != info[1]
                or linked != count
                or minimum != 0
                or maximum != count - 1
            ):
                raise sqlite3.DatabaseError("Data import manifest is incomplete.")
            placeholders = ",".join("?" for _ in record_keys)
            rows = connection.execute(
                "SELECT r.record_id FROM data_import_records AS m "
                "JOIN point_in_time_records AS r ON r.record_id = m.record_id "
                f"WHERE m.import_id = ? AND r.record_key IN ({placeholders}) "
                "ORDER BY m.ordinal",
                (import_id, *record_keys),
            ).fetchall()
            return tuple(str(row[0]) for row in rows)

    def _record_import(
        self,
        import_key: str,
        request_hash: str,
        records: tuple[DataRecord, ...],
        *,
        source_id: str,
        review_url: str,
        reviewed_at: datetime,
    ) -> DataImportResult:
        if RECORD_KEY.fullmatch(import_key) is None:
            raise ValueError("Invalid data import key.")
        if re.fullmatch(r"[0-9a-f]{64}", request_hash) is None:
            raise ValueError("Invalid data import request hash.")
        if not records:
            raise ValueError("Data import must contain at least one record.")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT import_id, request_hash, record_count "
                    "FROM data_imports WHERE import_key = ?",
                    (import_key,),
                ).fetchone()
                if existing is not None:
                    if existing[1] != request_hash:
                        raise DataImportConflict(
                            "Data import key already belongs to a different request."
                        )
                    rows = connection.execute(
                        "SELECT record_id FROM data_import_records "
                        "WHERE import_id = ? ORDER BY ordinal",
                        (existing[0],),
                    ).fetchall()
                    if len(rows) != existing[2]:
                        raise sqlite3.DatabaseError(
                            "Data import manifest is incomplete."
                        )
                    return DataImportResult(
                        str(existing[0]), tuple(str(r[0]) for r in rows)
                    )

                now = _utc(self.clock.now())
                if _utc(reviewed_at) > now:
                    raise ValueError("Source review time cannot be in the future.")
                import_id = secrets.token_urlsafe(18)
                connection.execute(
                    "INSERT INTO data_imports "
                    "(import_id, import_key, request_hash, source_id, review_url, "
                    "reviewed_at, reviewed_by, recorded_at, record_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'owner', ?, ?)",
                    (
                        import_id,
                        import_key,
                        request_hash,
                        source_id,
                        review_url,
                        _timestamp(reviewed_at),
                        _timestamp(now),
                        len(records),
                    ),
                )
                record_ids = []
                for ordinal, record in enumerate(records):
                    record_id = self._insert_record(connection, record, now)
                    record_ids.append(record_id)
                    connection.execute(
                        "INSERT INTO data_import_records VALUES (?, ?, ?)",
                        (import_id, ordinal, record_id),
                    )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="data.csv.imported",
                    object_type="data_import",
                    object_id=import_id,
                    payload={"request_hash": request_hash, "count": str(len(records))},
                )
                return DataImportResult(import_id, tuple(record_ids))

    @staticmethod
    def _insert_record(
        connection: sqlite3.Connection, record: DataRecord, now: datetime
    ) -> str:
        if _utc(record.known_at) > now:
            raise ValueError("Data record cannot be known in the future.")
        previous = connection.execute(
            "SELECT MAX(recorded_at) FROM point_in_time_records "
            "WHERE record_kind = ? AND record_key = ?",
            (record.kind.value, record.record_key),
        ).fetchone()
        if previous[0] is not None and _timestamp(now) <= previous[0]:
            raise ValueError("Data record time must advance for the same key.")
        record_id = secrets.token_urlsafe(18)
        payload_hash = sha256(record.payload_json.encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO point_in_time_records "
            "(record_id, record_kind, record_key, source_id, source_url, unit, "
            "event_at, published_at, known_at, received_at, quality_status, "
            "payload_json, payload_hash, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                record.kind.value,
                record.record_key,
                record.source_id,
                record.source_url,
                record.unit,
                _timestamp(record.event_at) if record.event_at else None,
                _timestamp(record.published_at) if record.published_at else None,
                _timestamp(record.known_at),
                _timestamp(record.received_at),
                record.quality_status.value,
                record.payload_json,
                payload_hash,
                _timestamp(now),
            ),
        )
        return record_id

    def record_for(
        self, kind: RecordKind, record_key: str, *, as_of: datetime
    ) -> DataRecord:
        return self.record_version_for(kind, record_key, as_of=as_of).record

    def record_version_for(
        self, kind: RecordKind, record_key: str, *, as_of: datetime
    ) -> DataRecordVersion:
        if not isinstance(kind, RecordKind):
            raise TypeError("Invalid data record kind.")
        if not isinstance(record_key, str) or RECORD_KEY.fullmatch(record_key) is None:
            raise ValueError("Invalid data record key.")
        as_of_text = _timestamp(as_of)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record_kind, record_key, source_id, source_url, unit, event_at, "
                "published_at, known_at, received_at, quality_status, payload_json, "
                "payload_hash, recorded_at, record_id FROM point_in_time_records "
                "WHERE record_kind = ? AND record_key = ? AND known_at <= ? "
                "AND recorded_at <= ? "
                "ORDER BY known_at DESC, sequence DESC LIMIT 1",
                (kind, record_key, as_of_text, as_of_text),
            ).fetchone()
        if row is None:
            raise MissingDataRecord(
                f"No {kind} record for {record_key} as of {as_of_text}."
            )
        return _decode_version(str(row[13]), row)

    def versions_for_sources(
        self,
        kind: RecordKind,
        record_key: str,
        *,
        source_ids: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[DataRecordVersion | None, ...]:
        if not isinstance(kind, RecordKind):
            raise TypeError("Invalid data record kind.")
        if not isinstance(record_key, str) or RECORD_KEY.fullmatch(record_key) is None:
            raise ValueError("Invalid data record key.")
        if (
            not isinstance(source_ids, tuple)
            or not 1 <= len(source_ids) <= 32
            or any(
                not isinstance(source, str) or RECORD_KEY.fullmatch(source) is None
                for source in source_ids
            )
            or len(set(source_ids)) != len(source_ids)
        ):
            raise ValueError("Expected 1 to 32 distinct data source identifiers.")
        as_of_text = _timestamp(as_of)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            versions: list[DataRecordVersion | None] = []
            for source in source_ids:
                row = connection.execute(
                    "SELECT record_kind, record_key, source_id, source_url, unit, event_at, "
                    "published_at, known_at, received_at, quality_status, payload_json, "
                    "payload_hash, recorded_at, record_id FROM point_in_time_records "
                    "WHERE record_kind = ? AND record_key = ? AND source_id = ? "
                    "AND known_at <= ? AND recorded_at <= ? "
                    "ORDER BY known_at DESC, sequence DESC LIMIT 1",
                    (kind, record_key, source, as_of_text, as_of_text),
                ).fetchone()
                versions.append(
                    _decode_version(str(row[13]), row) if row is not None else None
                )
            return tuple(versions)

    def version_for(self, record_id: str, *, as_of: datetime) -> DataRecordVersion:
        if (
            not isinstance(record_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", record_id) is None
        ):
            raise ValueError("Invalid data record identifier.")
        as_of_text = _timestamp(as_of)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record_kind, record_key, source_id, source_url, unit, event_at, "
                "published_at, known_at, received_at, quality_status, payload_json, "
                "payload_hash, recorded_at FROM point_in_time_records "
                "WHERE record_id = ? AND known_at <= ? AND recorded_at <= ?",
                (record_id, as_of_text, as_of_text),
            ).fetchone()
        if row is None:
            raise MissingDataRecord(
                f"No data record version {record_id} as of {as_of_text}."
            )
        return _decode_version(record_id, row)
