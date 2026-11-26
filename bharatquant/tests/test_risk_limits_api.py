import asyncio
from pathlib import Path

import httpx

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate
from bq.risk.policy import RiskLimits
from bq.settings import SettingsStore

PASSWORD = "synthetic owner password"
LIMITS = {
    "max_symbol_exposure_paise": 500_000,
    "max_open_positions": 5,
    "max_daily_loss_paise": 50_000,
    "max_weekly_loss_paise": 100_000,
    "max_monthly_loss_paise": 200_000,
    "max_order_rate_per_minute": 10,
}


def _app(tmp_path: Path):
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    return path, create_app(database_path=path)


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_risk_limits_require_auth_and_csrf(tmp_path: Path) -> None:
    _path, app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/settings/risk-limits")).status_code == 401
            headers = await _signed_in(client)
            assert (
                await client.put(
                    "/api/v1/settings/risk-limits",
                    json=LIMITS,
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            response = await client.put(
                "/api/v1/settings/risk-limits", json=LIMITS, headers=headers
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"

    asyncio.run(check())


def test_risk_limits_round_trip_and_reset(tmp_path: Path) -> None:
    path, app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            empty = await client.get("/api/v1/settings/risk-limits")
            assert empty.json()["configured"] is False
            assert empty.json()["max_symbol_exposure_paise"] is None

            saved = await client.put(
                "/api/v1/settings/risk-limits", json=LIMITS, headers=headers
            )
            assert saved.status_code == 200
            assert saved.json()["configured"] is True
            assert saved.json()["max_symbol_exposure_paise"] == 500_000

            loaded = await client.get("/api/v1/settings/risk-limits")
            assert loaded.json() == saved.json()
            persisted = SettingsStore(path).load().risk_limits
            assert persisted == RiskLimits(**LIMITS)

            cleared = await client.delete(
                "/api/v1/settings/risk-limits", headers=headers
            )
            assert cleared.status_code == 200
            assert cleared.json()["configured"] is False
            assert SettingsStore(path).load().risk_limits is None

    asyncio.run(check())


def test_risk_limits_reject_invalid_values(tmp_path: Path) -> None:
    _path, app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            for field in ("max_symbol_exposure_paise", "max_order_rate_per_minute"):
                response = await client.put(
                    "/api/v1/settings/risk-limits",
                    json={**LIMITS, field: -1},
                    headers=headers,
                )
                assert response.status_code == 422

    asyncio.run(check())
