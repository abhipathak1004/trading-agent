import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
BODY = {
    "strategy": "Buy the strongest momentum name with a clean R:R.",
    "symbols": "NSE:RELIANCE, NSE:TCS",
    "timeframe": "swing",
    "equity_paise": 1_000_000,
    "available_cash_paise": 500_000,
    "risk_basis_points": 100,
    "max_positions": 1,
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


class FakeRun:
    as_of = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    symbols: tuple[object, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": BODY["strategy"],
            "timeframe": "swing",
            "as_of": "2026-11-26T12:00:00+00:00",
            "spec": {
                "strategy": BODY["strategy"],
                "timeframe": "swing",
                "symbols": ["NSE:RELIANCE", "NSE:TCS"],
                "max_positions": 1,
                "equity_paise": 1_000_000,
                "available_cash_paise": 500_000,
                "risk_basis_points": 100,
                "execution": "PAPER_RESEARCH",
            },
            "symbols": [
                {
                    "requested": "NSE:RELIANCE",
                    "symbol": "NSE:RELIANCE",
                    "ok": True,
                    "error": None,
                    "signal": "BUY",
                    "composite": 0.4,
                    "mode": "RULES_ONLY",
                    "data_freshness": "delayed_yfinance",
                    "plan": None,
                    "agents": [],
                    "facts": [],
                    "vetoes": [],
                    "notes": [],
                    "model_routing": [],
                },
                {
                    "requested": "NSE:TCS",
                    "symbol": "NSE:TCS",
                    "ok": False,
                    "error": "No market data.",
                },
            ],
            "decision": {
                "strategy_summary": "One momentum candidate.",
                "ranked_actions": [
                    {
                        "symbol": "NSE:RELIANCE",
                        "action": "BUY",
                        "rank": 1,
                        "size_scale": 1.0,
                        "rationale": "Best composite.",
                    }
                ],
                "portfolio_notes": "One position.",
                "risk_notes": "Long-only.",
                "source": "rules",
                "model": None,
                "fallback_used": True,
                "fallback_from": "reasoning",
            },
            "accepted": [],
            "rejected": [],
            "notes": ["Paper research only."],
        }


class FakeSuperAgent:
    def __init__(self, **_kwargs: object) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> FakeRun:
        self.calls.append(kwargs)
        return FakeRun()


def test_strategy_route_requires_auth_and_csrf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    monkeypatch.setattr("bq.api.SuperAgent", FakeSuperAgent)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (
                await client.post("/api/v1/strategy/run", json=BODY)
            ).status_code == 401
            headers = await _signed_in(client)
            assert (
                await client.post(
                    "/api/v1/strategy/run",
                    json=BODY,
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            response = await client.post(
                "/api/v1/strategy/run", json=BODY, headers=headers
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"

    asyncio.run(check())


def test_strategy_route_returns_the_orchestration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    runner = FakeSuperAgent()
    monkeypatch.setattr("bq.api.SuperAgent", lambda **_kwargs: runner)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/strategy/run", json=BODY, headers=headers
            )
            assert response.status_code == 200
            body = response.json()
            assert body["decision"]["source"] == "rules"
            assert body["symbols"][0]["symbol"] == "NSE:RELIANCE"
            assert body["symbols"][1]["ok"] is False
            assert body["notes"] == ["Paper research only."]
            assert runner.calls[0]["max_positions"] == 1

    asyncio.run(check())


def test_strategy_route_rejects_bad_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    monkeypatch.setattr("bq.api.SuperAgent", FakeSuperAgent)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/strategy/run",
                json={**BODY, "max_positions": 0},
                headers=headers,
            )
            assert response.status_code == 422
            response = await client.post(
                "/api/v1/strategy/run",
                json={**BODY, "strategy": ""},
                headers=headers,
            )
            assert response.status_code == 422

    asyncio.run(check())
