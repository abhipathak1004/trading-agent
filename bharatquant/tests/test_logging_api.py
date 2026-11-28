import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate
from bq.logbook import LoggingConfig, configure_logbook, reset_logbook

PASSWORD = "synthetic owner password"


def _app(tmp_path: Path) -> FastAPI:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    return create_app(database_path=path)


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_logging_settings_require_auth_and_csrf(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/logs")).status_code == 401
            headers = await _signed_in(client)

            status = await client.get("/api/v1/logs")
            assert status.status_code == 200
            assert status.headers["cache-control"] == "no-store"
            body = status.json()
            assert body["enabled"] is True
            assert body["retention_days"] == 14
            assert body["flush_minutes"] == 15
            assert body["files"] == []
            assert body["recent"] == []

            without_csrf = await client.put(
                "/api/v1/logs",
                json={"enabled": True, "retention_days": 30, "flush_minutes": 15},
                headers={"Origin": ALLOWED_ORIGIN},
            )
            assert without_csrf.status_code == 403

            updated = await client.put(
                "/api/v1/logs",
                json={"enabled": False, "retention_days": 30, "flush_minutes": 5},
                headers=headers,
            )
            assert updated.status_code == 200
            assert updated.json()["enabled"] is False
            assert updated.json()["retention_days"] == 30
            assert updated.json()["flush_minutes"] == 5

            invalid = await client.put(
                "/api/v1/logs",
                json={"enabled": True, "retention_days": 0, "flush_minutes": 15},
                headers=headers,
            )
            assert invalid.status_code == 422

    try:
        asyncio.run(check())
    finally:
        reset_logbook()


def test_flush_endpoint_writes_recorded_entries(tmp_path: Path) -> None:
    app = _app(tmp_path)
    book = configure_logbook(tmp_path, LoggingConfig())
    book.record("system", message="api flush")

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post("/api/v1/logs/flush", headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body["written"] == 1
            assert len(body["files"]) == 1
            assert body["status"]["pending_bytes"] == 0
            assert len(body["status"]["files"]) == 1
            assert body["status"]["recent"][0]["data"]["message"] == "api flush"

    try:
        asyncio.run(check())
    finally:
        reset_logbook()
