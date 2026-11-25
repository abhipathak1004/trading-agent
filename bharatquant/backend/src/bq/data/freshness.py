from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from bq.data.records import DataRecord, DataRecordVersion, QualityStatus, RecordKind

MAX_THRESHOLD_SECONDS = 31_536_000


class DelayedDataMode(StrEnum):
    WARN = "WARN"
    BLOCK = "BLOCK"
    ALLOW = "ALLOW"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    DELAYED_WARN = "DELAYED_WARN"
    DELAYED_BLOCK = "DELAYED_BLOCK"
    DELAYED_ALLOW = "DELAYED_ALLOW"
    STALE_BLOCK = "STALE_BLOCK"
    QUALITY_BLOCK = "QUALITY_BLOCK"
    UNKNOWN_EVENT_TIME_BLOCK = "UNKNOWN_EVENT_TIME_BLOCK"


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    delayed_after: timedelta
    critical_after: timedelta
    delayed_mode: DelayedDataMode

    def __post_init__(self) -> None:
        if (
            not isinstance(self.delayed_after, timedelta)
            or not isinstance(self.critical_after, timedelta)
            or self.delayed_after < timedelta(0)
            or self.critical_after <= self.delayed_after
            or not isinstance(self.delayed_mode, DelayedDataMode)
        ):
            raise ValueError("Invalid freshness thresholds or delayed-data mode.")


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    status: FreshnessStatus
    age: timedelta | None


def freshness_policy_from_seconds(
    delayed_after: int, critical_after: int, mode: DelayedDataMode
) -> FreshnessPolicy:
    if (
        isinstance(delayed_after, bool)
        or not isinstance(delayed_after, int)
        or isinstance(critical_after, bool)
        or not isinstance(critical_after, int)
        or not 0 <= delayed_after < critical_after <= MAX_THRESHOLD_SECONDS
    ):
        raise ValueError("Freshness thresholds must be ordered within one year.")
    return FreshnessPolicy(
        timedelta(seconds=delayed_after), timedelta(seconds=critical_after), mode
    )


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Freshness timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def inspect_snapshot_freshness(
    version: DataRecordVersion, *, as_of: datetime, policy: FreshnessPolicy
) -> FreshnessResult:
    if not isinstance(version, DataRecordVersion) or not isinstance(
        policy, FreshnessPolicy
    ):
        raise ValueError("A retained snapshot and freshness policy are required.")
    instant = _utc(as_of)
    record = version.record
    if not isinstance(record, DataRecord) or record.kind is not RecordKind.SNAPSHOT:
        raise ValueError("Freshness inspection requires a snapshot record.")
    recorded_at = _utc(version.recorded_at)
    known_at = _utc(record.known_at)
    if recorded_at > instant or known_at > instant:
        raise ValueError("Snapshot was unavailable at the requested time.")
    if recorded_at < known_at:
        raise ValueError("Snapshot timestamps are inconsistent.")
    if record.event_at is None:
        return FreshnessResult(FreshnessStatus.UNKNOWN_EVENT_TIME_BLOCK, None)
    event_time = _utc(record.event_at)
    if event_time > instant:
        raise ValueError("Snapshot event time is after the requested time.")
    if event_time > _utc(record.received_at) or (
        record.published_at is not None and event_time > _utc(record.published_at)
    ):
        raise ValueError("Snapshot timestamps are inconsistent.")
    age = instant - event_time
    if record.quality_status is not QualityStatus.ACCEPTABLE:
        status = FreshnessStatus.QUALITY_BLOCK
    elif age > policy.critical_after:
        status = FreshnessStatus.STALE_BLOCK
    elif age > policy.delayed_after:
        status = FreshnessStatus(f"DELAYED_{policy.delayed_mode.value}")
    else:
        status = FreshnessStatus.FRESH
    return FreshnessResult(status, age)
