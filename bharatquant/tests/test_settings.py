import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate
from bq.infra.task_registry import TaskRegistry
from bq.model_routing import (
    DataClassification,
    ModelTier,
    TaskClass,
    TaskPolicy,
)
from bq.task_registry import TaskDefinition

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
PASSWORD = "synthetic owner password"


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _definition() -> TaskDefinition:
    return TaskDefinition(
        policy=TaskPolicy(
            "confidence.review",
            "v1",
            TaskClass.REASONING,
            frozenset(DataClassification),
            frozenset(ModelTier),
        ),
        prompt_version="prompt-v1",
        spec_version="spec-v1",
        system="Review the supplied evidence.",
        system_classification=DataClassification.PUBLIC,
        output_schema_json='{"type":"object"}',
        schema_classification=DataClassification.PUBLIC,
    )


def test_settings_overview_requires_auth_and_reports_configuration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    TaskRegistry(path, FrozenClock()).save(_definition())
    auth = OwnerAuth(path)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.get("/api/v1/settings/overview")
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"

            auth.setup(PASSWORD)
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": PASSWORD},
            )
            csrf = login.json()["csrf_token"]
            assert login.status_code == 200

            response = await client.get("/api/v1/settings/overview")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["task_count"] == 1
            assert body["tasks"][0]["task_id"] == "confidence.review"
            assert body["tasks"][0]["task_version"] == "v1"
            assert body["tasks"][0]["task_class"] == "REASONING"
            assert body["tasks"][0]["allowed_data"] == ["PRIVATE", "PUBLIC"]
            assert body["schedule_count"] == 0
            assert body["model_usage_groups"] == 0
            assert body["provider_connected"] is False
            assert body["api_keys_configured"] is False
            assert body["agent_configuration_state"] == "not_configured"
            assert isinstance(body["scheduler_running"], bool)

            logout = await client.post(
                "/api/v1/auth/logout",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert logout.status_code == 204
            assert (
                await client.get("/api/v1/settings/overview")
            ).status_code == 401

    asyncio.run(check())
