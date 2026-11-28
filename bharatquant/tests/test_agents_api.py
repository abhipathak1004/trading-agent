import asyncio
from pathlib import Path

import httpx

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


def test_agents_catalogue_requires_auth_and_returns_shape(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/agents")).status_code == 401
            headers = await _signed_in(client)
            response = await client.get("/api/v1/agents", headers=headers)
            assert response.status_code == 200
            body = response.json()
            ids = {item["agent_id"] for item in body["agents"]}
            assert "research" in ids
            assert "news_research" in ids
            assert "super_agent_cio" in ids
            research = next(
                item for item in body["agents"] if item["agent_id"] == "research"
            )
            assert research["kind"] == "loop"
            assert research["route"]["tier"] == "flash"
            assert "tools" in research

    asyncio.run(check())
