import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.cli import main
from bq.data.csv_eod import Adjustment, import_eod_csv
from bq.data.records import (
    DataImportConflict,
    DataImportResult,
    DataRecordStore,
    MissingDataRecord,
    RecordKind,
)
from bq.infra import database
from bq.infra.database import check_database, migrate

HEADER = b"instrument_id,trading_date,event_at,open,high,low,close,volume\n"
ROW = b"TEST,2026-11-20,2026-11-20T10:00:00Z,10.10,12,9,11.50,100\n"


class FrozenClock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


def _import(
    store: DataRecordStore,
    content: bytes = HEADER + ROW,
    *,
    import_key: str = "fixture",
    review_url: str = "https://example.test/terms",
    source_url: str = "https://example.test/data",
) -> DataImportResult:
    return import_eod_csv(
        store,
        content,
        import_key=import_key,
        source_id="fixture",
        source_url=source_url,
        review_url=review_url,
        reviewed_at=datetime(2026, 11, 20, tzinfo=UTC),
        currency="INR",
        adjustment=Adjustment.UNADJUSTED,
    )


def test_csv_import_is_atomic_idempotent_and_preserves_decimal_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = DataRecordStore(path, clock)
    first = _import(store)
    clock.instant += timedelta(hours=1)
    assert _import(store) == first
    with pytest.raises(DataImportConflict, match="different request"):
        _import(store, HEADER + ROW.replace(b"11.50", b"11.60"))

    record = store.record_for(
        RecordKind.EOD_BAR,
        "fixture/TEST/2026-11-20/UNADJUSTED",
        as_of=clock.now(),
    )
    assert record.payload == {
        "instrument_id": "TEST",
        "trading_date": "2026-11-20",
        "adjustment": "UNADJUSTED",
        "open": "10.10",
        "high": "12",
        "low": "9",
        "close": "11.50",
        "volume": 100,
    }
    with pytest.raises(MissingDataRecord):
        store.record_for(
            RecordKind.EOD_BAR,
            "fixture/TEST/2026-11-20/UNADJUSTED",
            as_of=clock.now() - timedelta(hours=1, microseconds=1),
        )
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM data_imports").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM point_in_time_records").fetchone()[
                0
            ]
            == 1
        )
        assert connection.execute(
            "SELECT review_url, reviewed_by FROM data_imports"
        ).fetchone() == ("https://example.test/terms", "owner")
        assert connection.execute("SELECT action FROM audit_events").fetchall() == [
            ("data.csv.imported",)
        ]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM data_import_records")
    check_database(path)


@pytest.mark.parametrize(
    "content",
    [
        b"wrong,header\n",
        HEADER,
        HEADER + ROW + ROW,
        HEADER + ROW.replace(b",12,9,", b",8,9,"),
        HEADER + ROW.replace(b",10.10,", b",NaN,"),
        HEADER + ROW.replace(b",100\n", b",1.5\n"),
        HEADER + ROW.replace(b"2026-11-20T10:00:00Z", b"2026-11-20T10:00:00"),
        HEADER + ROW.replace(b"2026-11-20T10:00:00Z", b"2026-11-22T10:00:00Z"),
        HEADER + ROW + b"bad,row\n",
        b"x" * (5 * 1024 * 1024 + 1),
    ],
)
def test_csv_rejects_bad_data_without_any_partial_import(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = DataRecordStore(path, FrozenClock())
    with pytest.raises(ValueError):
        _import(store, content)
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM data_imports").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM point_in_time_records").fetchone()[
                0
            ]
            == 0
        )


def test_csv_requires_owner_review_and_rolls_back_if_audit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = DataRecordStore(path, FrozenClock())
    with pytest.raises(ValueError, match="Review URL"):
        _import(store, review_url="https://owner:secret@example.test/terms")
    with pytest.raises(ValueError, match="Source URL"):
        _import(store, source_url="https://example.test/data?token=secret")

    def fail_audit(*args: object, **kwargs: object) -> None:
        raise sqlite3.DatabaseError("Synthetic audit failure")

    monkeypatch.setattr("bq.data.records.append_event", fail_audit)
    with pytest.raises(sqlite3.DatabaseError, match="Synthetic audit failure"):
        _import(store)
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM data_imports").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM point_in_time_records").fetchone()[
                0
            ]
            == 0
        )
    check_database(path)


def test_concurrent_retries_import_only_one_batch(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = DataRecordStore(path, FrozenClock())
    barrier = Barrier(2)

    def run() -> DataImportResult:
        barrier.wait()
        return _import(store)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run), executor.submit(run)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM data_imports").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM point_in_time_records").fetchone()[
                0
            ]
            == 1
        )
    check_database(path)


def test_csv_correction_preserves_the_prior_as_of_record(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = DataRecordStore(path, clock)
    _import(store)
    first_at = clock.now()
    clock.instant += timedelta(minutes=1)
    _import(store, HEADER + ROW.replace(b"11.50", b"11.60"), import_key="correction")
    key = "fixture/TEST/2026-11-20/UNADJUSTED"
    assert store.record_for(RecordKind.EOD_BAR, key, as_of=first_at).payload[
        "close"
    ] == ("11.50")
    assert (
        store.record_for(RecordKind.EOD_BAR, key, as_of=clock.now()).payload["close"]
        == "11.60"
    )


def test_context_migration_preserves_existing_csv_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrations = database.packaged_migrations()
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations[:8])
    assert migrate(path, create=True) == 8
    store = DataRecordStore(path, FrozenClock())
    original = _import(store)
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations)
    assert migrate(path) == len(migrations) - 8
    assert _import(store) == original
    assert store.import_summary().record_count == 1
    check_database(path)


def test_cli_import_requires_terms_confirmation_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("BQ_HOME", str(home))
    assert main(["init"]) == 0
    csv_file = tmp_path / "fixture.csv"
    csv_file.write_bytes(
        HEADER + b"TEST,2000-01-01,2000-01-01T10:00:00Z,10,12,9,11,100\n"
    )
    args = [
        "data",
        "import-eod-csv",
        str(csv_file),
        "--import-key",
        "fixture",
        "--source-id",
        "fixture",
        "--source-url",
        "https://example.test/data",
        "--review-url",
        "https://example.test/terms",
        "--reviewed-at",
        "2000-01-01T12:00:00Z",
        "--currency",
        "INR",
        "--adjustment",
        "UNADJUSTED",
    ]
    with pytest.raises(SystemExit) as missing_confirmation:
        main(args)
    assert missing_confirmation.value.code == 2
    assert main([*args, "--confirm-terms-review"]) == 0
    assert main([*args, "--confirm-terms-review"]) == 0
    assert "quality is UNASSESSED" in capsys.readouterr().out
    csv_file.write_bytes(
        HEADER + b"TEST,2000-01-01,2000-01-01T10:00:00Z,10,12,9,10,100\n"
    )
    assert main([*args, "--confirm-terms-review"]) == 1
    assert "different request" in capsys.readouterr().err
    with closing(sqlite3.connect(home / "bq.db")) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM data_imports").fetchone()[0] == 1
        )


def test_data_status_is_private_uncached_and_counts_imports(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.get("/api/v1/data/status")
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            password = "synthetic owner password"
            auth.setup(password)
            response = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": password},
            )
            assert response.status_code == 200
            csrf = response.json()["csrf_token"]
            response = await client.get("/api/v1/data/status")
            assert response.json() == {
                "import_count": 0,
                "imported_record_count": 0,
                "import_quality": "unassessed",
                "live_provider": "not_configured",
                "recent_imports": [],
            }
            _import(DataRecordStore(path, FrozenClock()))
            response = await client.get("/api/v1/data/status")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["imported_record_count"] == 1
            assert response.json()["import_count"] == 1
            recent = response.json()["recent_imports"]
            assert len(recent) == 1
            assert recent[0]["source_id"] == "fixture"
            assert recent[0]["record_count"] == 1
            assert recent[0]["recorded_at"] == "2026-11-21T15:00:00Z"
            assert "record_id" not in recent[0]
            assert (
                await client.post(
                    "/api/v1/auth/logout",
                    headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                )
            ).status_code == 204
            assert (await client.get("/api/v1/data/status")).status_code == 401

    asyncio.run(check())


def test_import_history_is_bounded_and_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = DataRecordStore(path, clock)
    for index in range(21):
        _import(store, import_key=f"fixture-{index}")
        clock.instant += timedelta(seconds=1)
    summary = store.import_summary()
    assert summary.import_count == 21
    assert summary.record_count == 21
    assert len(summary.recent_imports) == 20
    assert summary.recent_imports[0].recorded_at == clock.now() - timedelta(seconds=1)
    assert summary.recent_imports[-1].recorded_at == clock.now() - timedelta(seconds=20)
