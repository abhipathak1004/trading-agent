import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.evals.store import EvalStore
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
FIXTURE = Path(__file__).parent / "fixtures" / "eval_results_sample.json"


def _app(tmp_path: Path) -> tuple[FastAPI, EvalStore]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    return create_app(database_path=path), EvalStore(path)


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
    )


def test_eval_config_reports_providers_categories_and_tools(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with _client(app) as client:
            assert (await client.get("/api/v1/evals/config")).status_code == 401
            headers = await _signed_in(client)
            response = await client.get("/api/v1/evals/config")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert [p["model"] for p in body["providers"]] == [
                "gemma4:12b-mlx",
                "qwen3.5:9b",
                "granite4.2:8b",
            ]
            assert body["categories"] == [
                "corporate_action",
                "earnings",
                "edge_tool_calls",
                "noise_filtering",
                "rbi_macro",
            ]
            assert set(body["tools"]) == {"npx", "ollama"}
            assert body["active_run_id"] is None
            assert headers  # signed in

    asyncio.run(check())


def test_second_active_run_is_conflict(tmp_path: Path, monkeypatch) -> None:
    app, store = _app(tmp_path)
    # Seed an active run directly so we do not spawn Promptfoo.
    store.create_run(models=("Qwen3.5-9B",), categories=(), config_path="c")

    async def check() -> None:
        async with _client(app) as client:
            headers = await _signed_in(client)
            response = await client.post("/api/v1/evals/runs", json={}, headers=headers)
            assert response.status_code == 409
            assert "active" in response.json()["detail"].lower()

    asyncio.run(check())


def test_start_run_rejects_unknown_provider(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with _client(app) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/evals/runs",
                json={"providers": ["nope:nope"]},
                headers=headers,
            )
            assert response.status_code == 422

    asyncio.run(check())


def test_run_detail_cases_and_scorecard(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    run = store.create_run(
        models=("Gemma 4 12B (MLX)", "Qwen3.5-9B"),
        categories=("earnings", "noise_filtering"),
        config_path="evals/promptfooconfig.yaml",
    )
    store.mark_started(run.id)
    summaries, _ = _scored()
    store.save_scores(run.id, summaries)
    store.mark_completed(run.id, str(FIXTURE))
    run_dir = store.artifact_dir(run.id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "scorecard.csv").write_text("model,total_cases\nx,1\n", encoding="utf-8")

    async def check() -> None:
        async with _client(app) as client:
            await _signed_in(client)
            detail = await client.get(f"/api/v1/evals/runs/{run.id}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["status"] == "completed"
            assert {s["model"] for s in body["scores"]} == {
                "Gemma 4 12B (MLX)",
                "Qwen3.5-9B",
            }

            failed = await client.get(
                f"/api/v1/evals/runs/{run.id}/cases", params={"status": "failed"}
            )
            assert failed.status_code == 200
            assert failed.json()["total"] == 1
            assert failed.json()["cases"][0]["category"] == "noise_filtering"
            assert failed.json()["cases"][0]["failure_reasons"]

            by_model = await client.get(
                f"/api/v1/evals/runs/{run.id}/cases",
                params={"model": "Qwen3.5-9B", "status": "all"},
            )
            assert by_model.json()["total"] == 3

            csv_response = await client.get(
                f"/api/v1/evals/runs/{run.id}/scorecard.csv"
            )
            assert csv_response.status_code == 200
            assert csv_response.headers["content-type"].startswith("text/csv")

    asyncio.run(check())


def test_promote_updates_agent_routing_and_returns_diff(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BQ_CONFIG_DIR", str(tmp_path / "config"))
    app, store = _app(tmp_path)
    assert store.list_runs() == []

    async def check() -> None:
        async with _client(app) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/evals/promote",
                json={
                    "model": "qwen3.5:9b",
                    "agent_id": "news_research",
                    "provider": "CUSTOM",
                    "endpoint": "http://127.0.0.1:11434/v1",
                },
                headers=headers,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["before"]["model"] is None
            assert body["after"]["model"] == "qwen3.5:9b"
            assert body["after"]["provider"] == "CUSTOM"
            assert "news_research:" in body["yaml_after"]

            config = await client.get("/api/v1/evals/config")
            assert config.json()["custom_endpoint"] == "http://127.0.0.1:11434/v1"

            routing = await client.get("/api/v1/settings/config")
            route = routing.json()["agent_routing"]["news_research"]
            assert route["model"] == "qwen3.5:9b"
            assert route["provider"] == "CUSTOM"

            unknown = await client.post(
                "/api/v1/evals/promote",
                json={"model": "nope:1b", "agent_id": "news_research"},
                headers=headers,
            )
            assert unknown.status_code == 422

    asyncio.run(check())


def test_promote_rejects_non_loopback_http_endpoint(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with _client(app) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/evals/promote",
                json={
                    "model": "qwen3.5:9b",
                    "agent_id": "news_research",
                    "endpoint": "http://example.com/v1",
                },
                headers=headers,
            )
            assert response.status_code == 422

    asyncio.run(check())


def test_cancel_requires_csrf_and_cancels_active_run(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    run = store.create_run(models=("a",), categories=(), config_path="c")

    async def check() -> None:
        async with _client(app) as client:
            headers = await _signed_in(client)
            no_csrf = await client.post(
                f"/api/v1/evals/runs/{run.id}/cancel",
                headers={"Origin": ALLOWED_ORIGIN},
            )
            assert no_csrf.status_code == 403
            cancelled = await client.post(
                f"/api/v1/evals/runs/{run.id}/cancel", headers=headers
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"

    asyncio.run(check())


def _scored():
    from bq.evals.config import resolve_paths
    from bq.evals.scoring import analyze_results_file

    return analyze_results_file(resolve_paths().scorer, FIXTURE)


@pytest.mark.parametrize("status", ["all", "failed", "passed"])
def test_case_status_values_are_accepted(tmp_path: Path, status: str) -> None:
    app, store = _app(tmp_path)
    run = store.create_run(models=("a",), categories=(), config_path="c")
    store.mark_started(run.id)
    store.mark_completed(run.id, str(FIXTURE))

    async def check() -> None:
        async with _client(app) as client:
            await _signed_in(client)
            response = await client.get(
                f"/api/v1/evals/runs/{run.id}/cases", params={"status": status}
            )
            assert response.status_code == 200
            assert response.json()["status"] == status

    asyncio.run(check())
