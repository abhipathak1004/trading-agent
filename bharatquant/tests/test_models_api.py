import asyncio
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate
from bq.llm_client import LLMNotConfigured, client_for
from bq.settings import Provider

PASSWORD = "synthetic owner password"


def _body() -> dict[str, object]:
    return {
        "tiers": {
            "decision": {
                "provider": "TYPESAFE_JEV",
                "model": "jev-1.14",
                "temperature": 0.0,
                "max_tokens": 1,
                "timeout_ms": 3000,
                "max_attempts": 1,
                "fallback_tier": "flash",
            },
            "reasoning": {
                "provider": "GEMINI",
                "model": "gemini-2.5-pro",
                "temperature": 0.2,
                "max_tokens": 4096,
                "timeout_ms": 60000,
                "max_attempts": 3,
                "fallback_tier": None,
            },
            "flash": {
                "provider": "GEMINI",
                "model": "gemini-2.5-flash",
                "temperature": 0.1,
                "max_tokens": 2048,
                "timeout_ms": 60000,
                "max_attempts": 3,
                "fallback_tier": "reasoning",
            },
        },
        "agent_routing": {"bull_debater": {"tier": "reasoning"}},
        "decision_policy": {
            "min_choice_confidence": 0.55,
            "min_noul_probability": 0.6,
            "abstain_score_band": [2, 3],
            "llm_fallback_size_scale": 0.75,
            "heuristic_size_scale": 0.5,
        },
        "pipeline": {
            "debate_rounds": 1,
            "max_tool_rounds_per_agent": 8,
            "screener_min_score": 0.6,
            "final_decision_min_prob": 0.65,
            "execution_mode": "PAPER",
            "decision_fallback_order": ["flash", "reasoning", "heuristic"],
        },
        "decision_route": "auto",
    }


def test_models_settings_route_requires_csrf_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            assert (
                await client.put("/api/v1/settings/models", json=_body())
            ).status_code == 401
            auth.setup(PASSWORD)
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": PASSWORD},
            )
            csrf = login.json()["csrf_token"]

            missing_csrf = await client.put(
                "/api/v1/settings/models",
                headers={"Origin": ALLOWED_ORIGIN},
                json=_body(),
            )
            assert missing_csrf.status_code == 403

            saved = await client.put(
                "/api/v1/settings/models",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json=_body(),
            )
            assert saved.status_code == 200
            body = saved.json()
            assert body["tiers"]["decision"]["model"] == "jev-1.14"
            assert body["tiers"]["flash"]["model"] == "gemini-2.5-flash"
            assert body["agent_routing"]["bull_debater"]["tier"] == "reasoning"
            assert body["decision_route"] == "auto"
            assert body["pipeline"]["decision_fallback_order"] == [
                "flash",
                "reasoning",
                "heuristic",
            ]
            assert body["decision_policy"]["abstain_score_band"] == [2, 3]
            assert body["decision_preview"] == "heuristic"

            key = await client.put(
                "/api/v1/settings/api-keys/GEMINI",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json={"api_key": "AIza-synthetic-key-1234"},
            )
            assert key.status_code == 200
            assert key.json()["decision_preview"] == "flash"

            jev = await client.put(
                "/api/v1/settings/api-keys/TYPESAFE_JEV",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json={"api_key": "jev-synthetic-key-1234"},
            )
            assert jev.status_code == 200
            assert jev.json()["decision_preview"] == "jev"
            assert jev.json()["configured_tiers"] == [
                "decision",
                "reasoning",
                "flash",
            ]

    asyncio.run(check())


def test_models_settings_rejects_an_invalid_tier(tmp_path: Path) -> None:
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
            body = _body()
            tiers = body["tiers"]
            assert isinstance(tiers, dict)
            decision = tiers["decision"]
            assert isinstance(decision, dict)
            decision["provider"] = "GEMINI"
            response = await client.put(
                "/api/v1/settings/models",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json=body,
            )
            assert response.status_code == 422
            assert "decision tier" in response.json()["detail"]

    asyncio.run(check())


def test_client_for_refuses_typesafe_jev() -> None:
    with pytest.raises(LLMNotConfigured, match="decision"):
        client_for(Provider.TYPESAFE_JEV)
