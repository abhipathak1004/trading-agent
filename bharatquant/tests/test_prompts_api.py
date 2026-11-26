import asyncio
from pathlib import Path

import httpx

from bq.analysis.prompts import BULL_TEMPLATE
from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"


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
    token = response.json().get("csrf_token", "")
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": token}


def test_prompt_routes_require_auth_and_csrf(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/prompts")).status_code == 401
            headers = await _signed_in(client)
            assert (
                await client.put(
                    "/api/v1/prompts/bull_debater",
                    json={"system": "x", "template": BULL_TEMPLATE},
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/v1/prompts/bull_debater/reset",
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            assert (
                await client.get("/api/v1/prompts", headers=headers)
            ).status_code == 200

    asyncio.run(check())


def test_prompt_round_trip_and_reset(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            saved = await client.put(
                "/api/v1/prompts/bull_debater",
                json={"system": "CUSTOM BULL", "template": BULL_TEMPLATE},
                headers=headers,
            )
            assert saved.status_code == 200
            body = saved.json()
            assert body["system"] == "CUSTOM BULL"
            assert body["version"] == 1
            assert body["is_default"] is False

            listing = await client.get("/api/v1/prompts", headers=headers)
            prompts = listing.json()["prompts"]
            bull = next(item for item in prompts if item["agent_id"] == "bull_debater")
            assert bull["system"] == "CUSTOM BULL"

            reset = await client.post(
                "/api/v1/prompts/bull_debater/reset", headers=headers
            )
            assert reset.status_code == 200
            assert reset.json()["is_default"] is True
            assert reset.json()["version"] == 2

    asyncio.run(check())


def test_prompt_errors_are_typed(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            missing = await client.put(
                "/api/v1/prompts/not_an_agent",
                json={"system": "x", "template": ""},
                headers=headers,
            )
            assert missing.status_code == 404
            broken = await client.put(
                "/api/v1/prompts/bull_debater",
                json={"system": "x", "template": "Symbol: {symbol}"},
                headers=headers,
            )
            assert broken.status_code == 422
            assert "missing" in broken.json()["detail"]

    asyncio.run(check())
