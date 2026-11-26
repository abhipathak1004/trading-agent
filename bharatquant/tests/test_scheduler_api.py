import asyncio
from pathlib import Path

import httpx

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
SCHEDULE = {
    "job_id": "memory.resolve",
    "enabled": True,
    "timezone": "Asia/Kolkata",
    "local_time": "18:00",
    "window_start": "17:00",
    "window_end": "19:00",
    "wake_policy": "ALWAYS_RUN",
    "wake_within_seconds": None,
    "max_attempts": 2,
    "interval_minutes": None,
    "weekdays": [0, 1, 2, 3, 4, 5, 6],
    "dependencies": [],
}


def _app(tmp_path: Path):
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


def test_scheduler_catalogue_crud_dry_run_and_run(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/schedules")).status_code == 401
            headers = await _signed_in(client)
            catalogue = await client.get("/api/v1/schedules/catalogue")
            assert catalogue.status_code == 200
            assert {item["job_id"] for item in catalogue.json()["jobs"]} == {
                "memory.resolve",
                "news.ingest",
                "report.morning",
                "report.evening_accuracy",
                "paper.stops",
                "paper.square_off",
            }

            initial = await client.get("/api/v1/schedules")
            assert initial.status_code == 200
            assert initial.json() == {"version": 0, "schedules": []}

            created = await client.put(
                "/api/v1/schedules/memory-daily",
                json=SCHEDULE,
                headers=headers,
            )
            assert created.status_code == 200
            assert created.json()["schedule_id"] == "memory-daily"
            assert created.json()["version"] == 1
            assert len(created.json()["next_occurrences"]) == 5

            listed = await client.get("/api/v1/schedules")
            assert listed.json()["version"] == 1
            assert len(listed.json()["schedules"]) == 1

            dry = await client.post(
                "/api/v1/schedules/memory-daily/dry-run", headers=headers
            )
            assert dry.status_code == 200
            assert dry.json() == {
                "valid": True,
                "schedule_id": "memory-daily",
                "job_id": "memory.resolve",
            }

            run = await client.post(
                "/api/v1/schedules/memory-daily/run",
                json={"idempotency_key": "manual-1"},
                headers=headers,
            )
            assert run.status_code == 200
            run_body = run.json()
            assert run_body["job_id"] == "memory.resolve"
            assert run_body["status"] == "PENDING"

            duplicate = await client.post(
                "/api/v1/schedules/memory-daily/run",
                json={"idempotency_key": "manual-1"},
                headers=headers,
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["run_id"] == run_body["run_id"]

            history = await client.get("/api/v1/schedules/memory-daily/history")
            assert history.status_code == 200
            assert history.json()[0]["run_id"] == run_body["run_id"]

            disabled = await client.post(
                "/api/v1/schedules/memory-daily/enable",
                json={"enabled": False},
                headers=headers,
            )
            assert disabled.status_code == 200
            assert disabled.json()["enabled"] is False
            assert disabled.json()["next_occurrences"] == []

            unknown_job = await client.put(
                "/api/v1/schedules/other",
                json={**SCHEDULE, "job_id": "missing.job"},
                headers=headers,
            )
            assert unknown_job.status_code == 422

            no_csrf = await client.put(
                "/api/v1/schedules/other",
                json=SCHEDULE,
                headers={"Origin": ALLOWED_ORIGIN},
            )
            assert no_csrf.status_code == 403

    asyncio.run(check())
