from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "correct horse battery staple"


def _app(tmp_path: Path) -> tuple[FastAPI, Path]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    return create_app(database_path=path), path


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    """Log in and leave the session cookie on the client."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    token = response.json().get("csrf_token", "")
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": token}


def test_analysis_runs_requires_authentication(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            response = await client.get("/api/v1/analysis/runs")
            assert response.status_code == 401
            response = await client.get("/api/v1/analysis/vendors")
            assert response.status_code == 401

    import asyncio

    asyncio.run(check())


def test_run_post_requires_csrf(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            await _signed_in(client)
            body = {
                "symbol": "NSE:RELIANCE",
                "timeframe": "swing",
                "equity_paise": 1_000_000_00,
                "available_cash_paise": 500_000_00,
                "risk_basis_points": 100,
            }
            response = await client.post(
                "/api/v1/analysis/run",
                json=body,
                headers={"Origin": ALLOWED_ORIGIN},
            )
            # Without the CSRF header the gate rejects before any vendor call.
            assert response.status_code == 403
            assert response.json()["detail"] == "Invalid CSRF token"

    import asyncio

    asyncio.run(check())


def test_vendors_reports_kite_disabled_by_default(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            await _signed_in(client)
            response = await client.get(
                "/api/v1/analysis/vendors",
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["kite_enabled"] is False
            daily = next(
                chain for chain in payload["chains"] if chain["domain"] == "bars.daily"
            )
            assert daily["vendors"] == ["yahoo"]

    import asyncio

    asyncio.run(check())


def test_runs_list_is_empty_initially(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            await _signed_in(client)
            response = await client.get("/api/v1/analysis/runs")
            assert response.status_code == 200
            assert response.json()["runs"] == []

    import asyncio

    asyncio.run(check())


def test_unknown_run_id_is_404(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            await _signed_in(client)
            response = await client.get("/api/v1/analysis/runs/nope")
            assert response.status_code == 404

    import asyncio

    asyncio.run(check())


def test_analysis_responses_are_no_store(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            await _signed_in(client)
            response = await client.get("/api/v1/analysis/runs")
            assert response.headers.get("cache-control") == "no-store"

    import asyncio

    asyncio.run(check())


@pytest.mark.parametrize(
    "body",
    [
        {"symbol": "NSE:RELIANCE", "equity_paise": 0, "available_cash_paise": 1},
        {"symbol": "", "equity_paise": 1, "available_cash_paise": 1},
        {
            "symbol": "NSE:RELIANCE",
            "timeframe": "scalp",
            "equity_paise": 1,
            "available_cash_paise": 1,
        },
        {
            "symbol": "NSE:RELIANCE",
            "equity_paise": 1,
            "available_cash_paise": 1,
            "risk_basis_points": 20_000,
        },
    ],
)
def test_invalid_bodies_are_rejected(tmp_path: Path, body: dict[str, object]) -> None:
    """Validation happens before any vendor or model call is made."""
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/analysis/run",
                json=body,
                headers=headers,
            )
            assert response.status_code == 422

    import asyncio

    asyncio.run(check())


def test_an_index_symbol_is_refused_for_trading(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/analysis/run",
                json={
                    "symbol": "NIFTY 50",
                    "timeframe": "swing",
                    "equity_paise": 1_000_000_00,
                    "available_cash_paise": 1_000_000_00,
                    "risk_basis_points": 100,
                },
                headers=headers,
            )
            assert response.status_code == 422

    import asyncio

    asyncio.run(check())
