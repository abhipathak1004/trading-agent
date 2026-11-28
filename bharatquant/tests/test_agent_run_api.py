import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from bq.agent_run_store import AgentRunStore
from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def _app(tmp_path: Path):
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    AgentRunStore(path).record(
        agent_id="research",
        task="seed task",
        final_text="seed answer",
        turns=1,
        stopped=True,
        provider="gemini",
        model="gemini-2.5-pro",
        input_tokens=1,
        output_tokens=1,
        steps=(),
        started_at=NOW,
    )
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


def test_agent_run_history_requires_auth_and_lists(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/agent/runs")).status_code == 401
            headers = await _signed_in(client)
            response = await client.get("/api/v1/agent/runs", headers=headers)
            assert response.status_code == 200
            runs = response.json()["runs"]
            assert len(runs) == 1
            assert runs[0]["agent_id"] == "research"
            run_id = runs[0]["run_id"]
            detail = await client.get(f"/api/v1/agent/runs/{run_id}", headers=headers)
            assert detail.status_code == 200
            assert detail.json()["task"] == "seed task"

    asyncio.run(check())
