import asyncio
from pathlib import Path

import httpx

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate
from bq.infra.schedules import ScheduleStore

PASSWORD = "synthetic owner password"
JOB = "memory.resolve"


def _app(tmp_path: Path):
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    return path, create_app(database_path=path)


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_memory_schedule_is_absent_then_owner_enabled(tmp_path: Path) -> None:
    path, app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (
                await client.get("/api/v1/schedules/memory-resolve")
            ).status_code == 401
            headers = await _signed_in(client)
            empty = await client.get("/api/v1/schedules/memory-resolve")
            assert empty.json()["configured"] is False
            assert empty.json()["enabled"] is False

            assert (
                await client.put(
                    "/api/v1/schedules/memory-resolve",
                    json={"enabled": True},
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            enabled = await client.put(
                "/api/v1/schedules/memory-resolve",
                json={"enabled": True},
                headers=headers,
            )
            assert enabled.status_code == 200
            assert enabled.json()["configured"] is True
            assert enabled.json()["enabled"] is True
            assert enabled.json()["schedule_id"] == "memory-resolve-daily"

            store = ScheduleStore(path, available_jobs=frozenset({JOB}))
            saved = store.load().schedules
            assert len(saved) == 1
            assert saved[0].job_id == JOB
            assert saved[0].enabled is True

            status = await client.get("/api/v1/schedules/status")
            assert status.status_code == 200
            body = status.json()
            assert body["schedule_count"] == 1
            assert body["schedules"][0]["job_id"] == JOB
            assert body["schedules"][0]["enabled"] is True

            disabled = await client.put(
                "/api/v1/schedules/memory-resolve",
                json={"enabled": False},
                headers=headers,
            )
            assert disabled.status_code == 200
            assert disabled.json()["enabled"] is False
            assert store.load().schedules[0].enabled is False

    asyncio.run(check())
