import asyncio
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

import bq.cli as cli
from bq.api import create_app
from bq.cli import main
from bq.core import CurrencyPaise, Quantity, exchange_time
from bq.infra import database
from bq.infra.jobs import JobRunStore


def test_init_preserves_existing_state_and_doctor_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "state with spaces"
    monkeypatch.setenv("BQ_HOME", str(home))
    assert main(["init"]) == 0
    with closing(sqlite3.connect(home / "bq.db")) as connection, connection:
        connection.execute(
            "INSERT INTO system_events VALUES (?, ?, ?)",
            ("event-1", "2026-11-21T12:00:00Z", "TEST"),
        )

    assert main(["init"]) == 0
    assert main(["doctor"]) == 0
    expected = len(database.packaged_migrations())
    with closing(sqlite3.connect(home / "bq.db")) as connection:
        assert connection.execute("SELECT event_id FROM system_events").fetchall() == [
            ("event-1",)
        ]
        assert connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(version,) for version in range(1, expected + 1)]


def test_init_refuses_a_database_owned_by_another_application(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE other_app_data (value TEXT)")

    with pytest.raises(database.DatabaseError, match="non-BharatQuant"):
        database.migrate(path, create=True)


def test_failed_migration_rolls_back_its_schema_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    database.migrate(path, create=True)
    migrations = database.packaged_migrations()
    applied = len(migrations)
    migrations.append(
        database.Migration(
            applied + 1,
            f"{applied + 1:03d}_example.sql",
            b"CREATE TABLE example (id INTEGER); INSERT INTO absent VALUES (1);",
        )
    )
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations)

    with pytest.raises(sqlite3.OperationalError, match="no such table: absent"):
        database.migrate(path)
    with closing(sqlite3.connect(path)) as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'example'"
        ).fetchall()
        assert connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(version,) for version in range(1, applied + 1)]


def test_owner_migration_preserves_existing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrations = database.packaged_migrations()
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations[:1])
    assert database.migrate(path, create=True) == 1
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO system_events VALUES (?, ?, ?)",
            ("event-1", "2026-11-21T12:00:00Z", "TEST"),
        )
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations)
    assert database.migrate(path) == len(migrations) - 1
    database.check_database(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT event_id FROM system_events").fetchall() == [
            ("event-1",)
        ]
        assert connection.execute("SELECT * FROM owner_credentials").fetchall() == []


def test_schedule_migration_preserves_existing_job_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrations = database.packaged_migrations()
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations[:6])
    assert database.migrate(path, create=True) == 6
    store = JobRunStore(path)
    run = store.enqueue(
        "synthetic",
        "first",
        scheduled_at=datetime(2026, 11, 21, tzinfo=UTC),
        max_attempts=1,
    )
    monkeypatch.setattr(database, "packaged_migrations", lambda: migrations)
    assert database.migrate(path) == len(migrations) - 6
    assert store.get(run.run_id) == run
    database.check_database(path)


def test_doctor_rejects_edited_migration_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    assert main(["init"]) == 0
    with closing(sqlite3.connect(tmp_path / "bq.db")) as connection, connection:
        connection.execute("UPDATE schema_migrations SET checksum = 'different'")
    assert main(["doctor"]) == 1


def test_loopback_health_does_not_claim_trading_readiness() -> None:
    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()),
            base_url="http://127.0.0.1:8000",
        ) as client:
            url = "/api/v1/system/health"
            response = await client.get(url)
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            response = await client.get(url, headers={"Host": "untrusted.invalid"})
            assert response.status_code == 400

    asyncio.run(check())


def test_web_build_is_served_without_capturing_unknown_api_paths(
    tmp_path: Path,
) -> None:
    web_root = tmp_path / "web"
    assets = web_root / "assets"
    assets.mkdir(parents=True)
    (web_root / "index.html").write_text(
        '<main>BharatQuant</main><script src="/assets/app.js"></script>'
        "<script>window.test = true</script>"
    )
    (assets / "app.js").write_text("fetch('/api/v1/system/health')")

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(web_root)),
            base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert "BharatQuant" in response.text
            policy = response.headers["content-security-policy"]
            nonce = re.search(r"'nonce-([^']+)'", policy)
            assert nonce is not None
            script_tags = re.findall(r"<script\b[^>]*>", response.text)
            assert len(script_tags) == 2
            assert all(f'nonce="{nonce.group(1)}"' in tag for tag in script_tags)
            assert "default-src 'none'" in policy
            assert "'strict-dynamic'" in policy
            assert "'unsafe-inline'" not in policy
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            response = await client.get("/index.html")
            assert response.status_code == 200
            second_policy = response.headers["content-security-policy"]
            second_nonce = re.search(r"'nonce-([^']+)'", second_policy)
            assert second_nonce is not None
            assert second_nonce.group(1) != nonce.group(1)
            assert f'nonce="{second_nonce.group(1)}"' in response.text
            response = await client.get("/assets/app.js")
            assert response.status_code == 200
            assert "system/health" in response.text
            response = await client.get("/api/v1/missing")
            assert response.status_code == 404

    asyncio.run(check())


def test_serve_rejects_an_incomplete_frontend_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BQ_HOME", str(tmp_path / "home"))
    assert main(["init"]) == 0
    web_root = tmp_path / "static"
    web_root.mkdir()
    monkeypatch.setattr(cli, "WEB_ROOT", web_root)

    def unexpected_start(*args: object, **kwargs: object) -> None:
        pytest.fail("Server started without a frontend index")

    monkeypatch.setattr(uvicorn, "run", unexpected_start)
    assert main(["serve"]) == 1
    assert "Frontend build missing" in capsys.readouterr().err


def test_exchange_time_preserves_instant_and_handles_date_rollover() -> None:
    instant = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)
    converted = exchange_time(instant)
    assert converted == instant
    assert converted.day == 22
    assert converted.utcoffset() == timedelta(hours=5, minutes=30)
    with pytest.raises(ValueError, match="timezone-aware"):
        exchange_time(datetime(2026, 11, 21, 20, 0))


@pytest.mark.parametrize("value_type", [CurrencyPaise, Quantity])
def test_money_and_quantity_require_nonnegative_integers(
    value_type: type[CurrencyPaise] | type[Quantity],
) -> None:
    assert value_type(0).value == 0
    assert value_type(123).value == 123
    with pytest.raises(ValueError, match="must not be negative"):
        value_type(-1)
    with pytest.raises(TypeError, match="must be an integer"):
        value_type(True)


@pytest.mark.parametrize("value_type", [CurrencyPaise, Quantity])
@pytest.mark.parametrize("invalid", [1.0, 1.5, "1", None])
def test_money_and_quantity_never_coerce_input(
    value_type: type[CurrencyPaise] | type[Quantity], invalid: int
) -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        value_type(invalid)
