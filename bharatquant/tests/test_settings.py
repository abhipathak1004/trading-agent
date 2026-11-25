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
            assert body["config"]["provider"] == "GEMINI"
            assert body["config"]["model_name"] == ""
            assert body["config"]["api_keys"] == []
            assert body["config"]["news_feed_url"].startswith("https://")
            assert body["config"]["model_route_configured"] is False

            logout = await client.post(
                "/api/v1/auth/logout",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert logout.status_code == 204
            assert (await client.get("/api/v1/settings/overview")).status_code == 401

    asyncio.run(check())


def test_settings_config_is_editable_with_csrf_and_origin(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            auth.setup(PASSWORD)
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": PASSWORD},
            )
            csrf = login.json()["csrf_token"]

            assert (await client.get("/api/v1/settings/config")).status_code == 200

            payload = {
                "provider": "DEEPSEEK",
                "model_name": "deepseek-chat",
                "news_feed_url": "https://example.com/feed.xml",
                "news_memory_ttl_days": 30,
            }
            # Missing CSRF is rejected.
            missing = await client.put(
                "/api/v1/settings/config",
                headers={"Origin": ALLOWED_ORIGIN},
                json=payload,
            )
            assert missing.status_code == 403
            # Wrong origin is rejected.
            bad_origin = await client.put(
                "/api/v1/settings/config",
                headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf},
                json=payload,
            )
            assert bad_origin.status_code == 403

            saved = await client.put(
                "/api/v1/settings/config",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json=payload,
            )
            assert saved.status_code == 200
            body = saved.json()
            assert body["provider"] == "DEEPSEEK"
            assert body["model_name"] == "deepseek-chat"
            assert body["news_memory_ttl_days"] == 30
            assert body["model_route_configured"] is False

            # An invalid feed URL is rejected with 422.
            bad = await client.put(
                "/api/v1/settings/config",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json={**payload, "news_feed_url": "http://insecure.example/feed.xml"},
            )
            assert bad.status_code == 422

            # Setting the matching provider key completes the route.
            key = await client.put(
                "/api/v1/settings/api-keys/DEEPSEEK",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json={"api_key": "sk-deepseek-synthetic-1234"},
            )
            assert key.status_code == 200
            body = key.json()
            assert body["model_route_configured"] is True
            assert body["api_keys"][0]["configured"] is True
            assert "sk-deepseek-synthetic-1234" not in str(body)

            overview = (await client.get("/api/v1/settings/overview")).json()
            assert overview["provider_connected"] is True
            assert overview["api_keys_configured"] is True
            assert overview["agent_configuration_state"] == "configured"

            cleared = await client.delete(
                "/api/v1/settings/api-keys/DEEPSEEK",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert cleared.status_code == 200
            assert cleared.json()["api_keys"] == []
            assert cleared.json()["model_route_configured"] is False

    asyncio.run(check())
