import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.cli import main
from bq.data.freshness import (
    DelayedDataMode,
    FreshnessPolicy,
    FreshnessStatus,
    inspect_snapshot_freshness,
)
from bq.data.records import (
    DataRecord,
    DataRecordStore,
    DataRecordVersion,
    QualityStatus,
    RecordKind,
)
from bq.infra.database import migrate

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
POLICY = FreshnessPolicy(
    timedelta(seconds=60), timedelta(seconds=180), DelayedDataMode.WARN
)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def snapshot(*, event_at: datetime | None, quality: QualityStatus) -> DataRecord:
    return DataRecord.create(
        kind=RecordKind.SNAPSHOT,
        record_key="fixture",
        source_id="fixture",
        source_url="https://example.test/fixture",
        event_at=event_at,
        received_at=NOW,
        known_at=NOW,
        quality_status=quality,
        payload={"price": "100.00"},
    )


def test_snapshot_freshness_uses_retained_version_and_exact_boundaries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = DataRecordStore(path, FrozenClock())
    record_id = store.record(
        snapshot(event_at=NOW - timedelta(seconds=60), quality=QualityStatus.ACCEPTABLE)
    )
    version = store.version_for(record_id, as_of=NOW)
    assert (
        inspect_snapshot_freshness(version, as_of=NOW, policy=POLICY).status
        is FreshnessStatus.FRESH
    )
    delayed = inspect_snapshot_freshness(
        version, as_of=NOW + timedelta(microseconds=1), policy=POLICY
    )
    assert delayed.status is FreshnessStatus.DELAYED_WARN
    assert delayed.age == timedelta(seconds=60, microseconds=1)
    blocked_policy = replace(POLICY, delayed_mode=DelayedDataMode.BLOCK)
    assert (
        inspect_snapshot_freshness(
            version, as_of=NOW + timedelta(seconds=1), policy=blocked_policy
        ).status
        is FreshnessStatus.DELAYED_BLOCK
    )
    allow_policy = replace(POLICY, delayed_mode=DelayedDataMode.ALLOW)
    assert (
        inspect_snapshot_freshness(
            version, as_of=NOW + timedelta(seconds=1), policy=allow_policy
        ).status
        is FreshnessStatus.DELAYED_ALLOW
    )
    assert (
        inspect_snapshot_freshness(
            version, as_of=NOW + timedelta(seconds=121), policy=allow_policy
        ).status
        is FreshnessStatus.STALE_BLOCK
    )
    assert (
        inspect_snapshot_freshness(
            version, as_of=NOW + timedelta(seconds=120), policy=POLICY
        ).status
        is FreshnessStatus.DELAYED_WARN
    )
    assert (
        inspect_snapshot_freshness(
            version, as_of=NOW + timedelta(seconds=120, microseconds=1), policy=POLICY
        ).status
        is FreshnessStatus.STALE_BLOCK
    )


def test_unassessed_and_unknown_time_are_blocked() -> None:
    unassessed = DataRecordVersion(
        "id", NOW, snapshot(event_at=NOW, quality=QualityStatus.UNASSESSED)
    )
    assert (
        inspect_snapshot_freshness(unassessed, as_of=NOW, policy=POLICY).status
        is FreshnessStatus.QUALITY_BLOCK
    )
    unknown = DataRecordVersion(
        "id", NOW, snapshot(event_at=None, quality=QualityStatus.ACCEPTABLE)
    )
    assert (
        inspect_snapshot_freshness(unknown, as_of=NOW, policy=POLICY).status
        is FreshnessStatus.UNKNOWN_EVENT_TIME_BLOCK
    )


def test_future_or_invalid_snapshot_evidence_is_rejected() -> None:
    record = snapshot(event_at=NOW, quality=QualityStatus.ACCEPTABLE)
    with pytest.raises(ValueError, match="unavailable"):
        inspect_snapshot_freshness(
            DataRecordVersion("id", NOW + timedelta(seconds=1), record),
            as_of=NOW,
            policy=POLICY,
        )
    with pytest.raises(ValueError, match="after"):
        inspect_snapshot_freshness(
            DataRecordVersion(
                "id", NOW, replace(record, event_at=NOW + timedelta(seconds=1))
            ),
            as_of=NOW,
            policy=POLICY,
        )
    with pytest.raises(ValueError, match="inconsistent"):
        inspect_snapshot_freshness(
            DataRecordVersion(
                "id", NOW, replace(record, event_at=NOW + timedelta(seconds=1))
            ),
            as_of=NOW + timedelta(seconds=2),
            policy=POLICY,
        )
    with pytest.raises(ValueError, match="inconsistent"):
        inspect_snapshot_freshness(
            DataRecordVersion("id", NOW - timedelta(seconds=1), record),
            as_of=NOW,
            policy=POLICY,
        )
    with pytest.raises(ValueError, match="inconsistent"):
        inspect_snapshot_freshness(
            DataRecordVersion(
                "id", NOW, replace(record, published_at=NOW - timedelta(seconds=1))
            ),
            as_of=NOW,
            policy=POLICY,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        inspect_snapshot_freshness(
            DataRecordVersion("id", NOW, record),
            as_of=NOW.replace(tzinfo=None),
            policy=POLICY,
        )
    with pytest.raises(ValueError, match="thresholds"):
        FreshnessPolicy(
            timedelta(seconds=60), timedelta(seconds=60), DelayedDataMode.WARN
        )


def test_snapshot_cli_reports_only_retained_metadata_and_explicit_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = DataRecordStore(path, FrozenClock())
    record_id = store.record(
        snapshot(event_at=NOW - timedelta(seconds=61), quality=QualityStatus.ACCEPTABLE)
    )
    arguments = [
        "data",
        "inspect-snapshot",
        record_id,
        "--as-of",
        NOW.isoformat(),
        "--delayed-after-seconds",
        "60",
        "--critical-after-seconds",
        "180",
        "--delayed-mode",
        "WARN",
    ]
    assert main(arguments) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {
        "record_id": record_id,
        "source_id": "fixture",
        "quality_status": "ACCEPTABLE",
        "as_of": NOW.isoformat(),
        "status": "DELAYED_WARN",
        "age_microseconds": "61000000",
        "delayed_after_seconds": 60,
        "critical_after_seconds": 180,
        "delayed_mode": "WARN",
    }
    unknown_id = store.record(
        replace(
            snapshot(event_at=None, quality=QualityStatus.UNASSESSED),
            record_key="unknown",
        )
    )
    arguments[2] = unknown_id
    assert main(arguments) == 0
    unknown = json.loads(capsys.readouterr().out)
    assert unknown["status"] == "UNKNOWN_EVENT_TIME_BLOCK"
    assert unknown["age_microseconds"] is None
    arguments[arguments.index("180")] = "60"
    assert main(arguments) == 1
    assert "thresholds" in capsys.readouterr().err


def test_snapshot_freshness_api_is_private_and_reports_selected_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    auth.setup("synthetic owner password")
    store = DataRecordStore(path, FrozenClock())
    record_id = store.record(
        snapshot(event_at=NOW - timedelta(seconds=61), quality=QualityStatus.ACCEPTABLE)
    )
    unknown_id = store.record(
        replace(
            snapshot(event_at=None, quality=QualityStatus.UNASSESSED),
            record_key="unknown",
        )
    )
    unassessed_id = store.record(
        replace(
            snapshot(event_at=NOW, quality=QualityStatus.UNASSESSED),
            record_key="unassessed",
        )
    )
    non_snapshot_id = store.record(
        replace(
            snapshot(event_at=NOW, quality=QualityStatus.ACCEPTABLE),
            kind=RecordKind.MARKET_STATISTIC,
            record_key="other-kind",
        )
    )
    app = create_app(database_path=path)
    params = {
        "record_id": record_id,
        "as_of": NOW.isoformat(),
        "delayed_after_seconds": "60",
        "critical_after_seconds": "180",
        "delayed_mode": "WARN",
    }

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.get(
                "/api/v1/data/snapshot-freshness", params=params
            )
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            response = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": "synthetic owner password"},
            )
            assert response.status_code == 200
            csrf = response.json()["csrf_token"]
            response = await client.get(
                "/api/v1/data/snapshot-freshness", params=params
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {
                "record_id": record_id,
                "source_id": "fixture",
                "quality_status": "ACCEPTABLE",
                "event_at": (NOW - timedelta(seconds=61))
                .isoformat()
                .replace("+00:00", "Z"),
                "known_at": "2026-11-22T10:00:00Z",
                "recorded_at": "2026-11-22T10:00:00Z",
                "as_of": "2026-11-22T10:00:00Z",
                "status": "DELAYED_WARN",
                "age_microseconds": "61000000",
                "delayed_after_seconds": 60,
                "critical_after_seconds": 180,
                "delayed_mode": "WARN",
            }
            response = await client.get(
                "/api/v1/data/snapshot-freshness",
                params={**params, "record_id": unknown_id},
            )
            assert response.json()["status"] == "UNKNOWN_EVENT_TIME_BLOCK"
            assert response.json()["age_microseconds"] is None
            response = await client.get(
                "/api/v1/data/snapshot-freshness",
                params={**params, "record_id": unassessed_id},
            )
            assert response.json()["status"] == "QUALITY_BLOCK"
            response = await client.get(
                "/api/v1/data/snapshot-freshness",
                params={**params, "delayed_mode": "BLOCK"},
            )
            assert response.json()["status"] == "DELAYED_BLOCK"
            response = await client.get(
                "/api/v1/data/snapshot-freshness",
                params={**params, "delayed_mode": "ALLOW"},
            )
            assert response.json()["status"] == "DELAYED_ALLOW"
            response = await client.get(
                "/api/v1/data/snapshot-freshness",
                params={**params, "delayed_mode": "ALLOW", "record_id": unassessed_id},
            )
            assert response.json()["status"] == "QUALITY_BLOCK"
            response = await client.get(
                "/api/v1/data/snapshot-freshness",
                params={**params, "delayed_mode": "ALLOW", "record_id": unknown_id},
            )
            assert response.json()["status"] == "UNKNOWN_EVENT_TIME_BLOCK"
            response = await client.get(
                "/api/v1/data/snapshot-freshness",
                params={
                    **params,
                    "delayed_mode": "ALLOW",
                    "as_of": (NOW + timedelta(seconds=120)).isoformat(),
                },
            )
            assert response.json()["status"] == "STALE_BLOCK"
            for overrides in (
                {"record_id": "missing"},
                {"as_of": (NOW - timedelta(seconds=1)).isoformat()},
            ):
                response = await client.get(
                    "/api/v1/data/snapshot-freshness", params={**params, **overrides}
                )
                assert response.status_code == 404
            for overrides in (
                {"record_id": "bad id"},
                {"record_id": non_snapshot_id},
                {"as_of": "2026-11-22T10:00:00"},
                {"delayed_mode": "INVALID"},
                {"critical_after_seconds": "60"},
                {"critical_after_seconds": "31536001"},
                {"delayed_after_seconds": "-1"},
            ):
                response = await client.get(
                    "/api/v1/data/snapshot-freshness", params={**params, **overrides}
                )
                assert response.status_code == 422
            assert (
                await client.post(
                    "/api/v1/auth/logout",
                    headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                )
            ).status_code == 204
            assert (
                await client.get("/api/v1/data/snapshot-freshness", params=params)
            ).status_code == 401

    asyncio.run(check())
