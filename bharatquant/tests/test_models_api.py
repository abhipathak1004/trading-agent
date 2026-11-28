import asyncio
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.evals.store import EvalStore
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


def _local_app(tmp_path: Path):
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    return create_app(database_path=path), EvalStore(path)


async def _local_session(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_local_model_registry_seeds_and_round_trips(tmp_path: Path) -> None:
    app, _ = _local_app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/models")).status_code == 401
            headers = await _local_session(client)
            seeded = await client.get("/api/v1/models")
            assert seeded.status_code == 200
            assert [m["model"] for m in seeded.json()["models"]] == [
                "gemma4:12b-mlx",
                "qwen3.5:9b",
                "granite4.2:8b",
            ]

            created = await client.post(
                "/api/v1/models",
                json={
                    "label": "Llama 3.3 70B",
                    "kind": "ollama",
                    "model": "llama3.3:70b",
                },
                headers=headers,
            )
            assert created.status_code == 201, created.text
            assert created.json()["id"] == "ollama:chat:llama3.3:70b"

            duplicate = await client.post(
                "/api/v1/models",
                json={"label": "again", "kind": "ollama", "model": "llama3.3:70b"},
                headers=headers,
            )
            assert duplicate.status_code == 409

            bad = await client.post(
                "/api/v1/models",
                json={"label": "bad", "kind": "ollama", "model": "has space"},
                headers=headers,
            )
            assert bad.status_code == 422

            remote = await client.post(
                "/api/v1/models",
                json={
                    "label": "Remote",
                    "kind": "openai_compatible",
                    "model": "remote",
                    "endpoint": "http://example.com/v1",
                },
                headers=headers,
            )
            assert remote.status_code == 422

            removed = await client.delete(
                "/api/v1/models",
                params={"model_id": "ollama:chat:llama3.3:70b"},
                headers=headers,
            )
            assert removed.status_code == 200
            assert "llama3.3:70b" not in [m["model"] for m in removed.json()["models"]]

    asyncio.run(check())


def test_clearing_the_registry_stays_empty(tmp_path: Path) -> None:
    app, _ = _local_app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _local_session(client)
            seeded = (await client.get("/api/v1/models")).json()["models"]
            for model in seeded:
                response = await client.delete(
                    "/api/v1/models",
                    params={"model_id": model["id"]},
                    headers=headers,
                )
                assert response.status_code == 200
            assert (await client.get("/api/v1/models")).json()["models"] == []
            # Never re-seeds the shipped set after an explicit empty save.
            assert (await client.get("/api/v1/models")).json()["models"] == []

    asyncio.run(check())


def test_pull_rejects_non_ollama_and_starts_for_ollama(
    tmp_path: Path, monkeypatch
) -> None:
    app, _ = _local_app(tmp_path)

    def fake_start(self, model):
        from bq.local_models import ModelPullError, PullState

        if model.kind != "ollama":
            raise ModelPullError("Only Ollama models can be pulled here.")
        return PullState("queued")

    monkeypatch.setattr(
        "bq.local_models.ModelPullRunner.start", fake_start, raising=True
    )

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _local_session(client)
            await client.post(
                "/api/v1/models",
                json={
                    "label": "MLX server",
                    "kind": "openai_compatible",
                    "model": "mlx-community/qwen",
                    "endpoint": "http://127.0.0.1:8080/v1",
                },
                headers=headers,
            )
            conflict = await client.post(
                "/api/v1/models/pull",
                json={"model_id": "openai:chat:mlx-community/qwen"},
                headers=headers,
            )
            assert conflict.status_code == 409
            started = await client.post(
                "/api/v1/models/pull",
                json={"model_id": "ollama:chat:gemma4:12b-mlx"},
                headers=headers,
            )
            assert started.status_code == 200
            assert started.json()["status"] == "queued"

    asyncio.run(check())


def test_eval_run_writes_a_generated_config(tmp_path: Path, monkeypatch) -> None:
    app, store = _local_app(tmp_path)
    monkeypatch.setattr(
        "bq.evals.runner.EvalRunner.start", lambda self, launch: None, raising=True
    )

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _local_session(client)
            response = await client.post(
                "/api/v1/evals/runs",
                json={
                    "providers": ["ollama:chat:gemma4:12b-mlx"],
                    "categories": ["earnings"],
                },
                headers=headers,
            )
            assert response.status_code == 201, response.text
            run_id = response.json()["id"]
            config = store.artifact_dir(run_id) / "promptfooconfig.yaml"
            assert config.is_file()
            text = config.read_text(encoding="utf-8")
            assert "  - id: ollama:chat:gemma4:12b-mlx" in text
            assert "  - id: ollama:chat:qwen3.5:9b" not in text

    asyncio.run(check())
