import asyncio
from pathlib import Path

import httpx

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
ACCOUNT = {
    "account_id": "paper-main",
    "name": "Paper Main",
    "opening_balance_paise": 10_000_000,
}
LIMITS = {
    "max_symbol_exposure_paise": 5_000_000,
    "max_open_positions": 5,
    "max_daily_loss_paise": 500_000,
    "max_weekly_loss_paise": 1_000_000,
    "max_monthly_loss_paise": 2_000_000,
    "max_order_rate_per_minute": 10,
}


def _body() -> dict[str, object]:
    plan = {
        "symbol": "NSE:RELIANCE",
        "exchange": "NSE",
        "timeframe": "swing",
        "signal": "BUY",
        "as_of": "2026-11-25T10:00:00Z",
        "data_freshness": "delayed_yfinance",
        "entry_price": "100.00",
        "stop_loss": "95.00",
        "target_price": "115.00",
        "suggested_quantity": 10,
        "confidence": 0.6,
        "order_type": "LIMIT",
        "product": "CNC",
        "invalidation_reason": "Closes below 95.",
    }
    return {
        "account_id": "paper-main",
        "plan": plan,
        "mode": "PAPER",
        "limits": LIMITS,
        "kill_switch": "NONE",
        "session_open_override": True,
    }


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_approval_realism_and_stop_surface(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            assert (
                await client.post(
                    "/api/v1/paper/accounts", json=ACCOUNT, headers=headers
                )
            ).status_code == 200
            placed = await client.post(
                "/api/v1/execution/paper",
                json={
                    **_body(),
                    "client_order_id": "approval-buy",
                    "product": "MIS",
                    "require_approval": True,
                },
                headers=headers,
            )
            assert placed.status_code == 200
            order = placed.json()["order"]
            assert order["approval_status"] == "PENDING"
            assert order["product"] == "MIS"
            assert (
                await client.post(
                    f"/api/v1/paper/orders/{order['order_id']}/fill",
                    json={
                        "quantity": 10,
                        "price_paise": 10_000,
                        "idempotency_key": "blocked-fill",
                    },
                    headers=headers,
                )
            ).status_code == 422

            decisions = await client.post(
                f"/api/v1/paper/orders/{order['order_id']}/approval",
                json={"approved": True, "note": "owner reviewed"},
                headers=headers,
            )
            assert decisions.status_code == 200
            assert decisions.json()["approval_status"] == "APPROVED"
            filled = await client.post(
                f"/api/v1/paper/orders/{order['order_id']}/fill",
                json={
                    "quantity": 10,
                    "price_paise": 10_000,
                    "idempotency_key": "approved-fill",
                },
                headers=headers,
            )
            assert filled.status_code == 200
            fill_body = filled.json()["fill"]
            assert int(fill_body["gross_paise"]) == 100_000
            assert int(fill_body["charges_paise"]) > 0

            realism = await client.get("/api/v1/paper/accounts/paper-main/realism")
            assert realism.status_code == 200
            realism_body = realism.json()
            assert realism_body["product_positions"] == [
                {"instrument_id": "NSE:RELIANCE", "product": "MIS", "quantity": 10}
            ]
            assert len(realism_body["tax_lots"]) == 1
            assert realism_body["approvals"][0]["status"] == "APPROVED"
            assert int(realism_body["charges_paise"]) > 0

            stop = await client.post(
                "/api/v1/paper/accounts/paper-main/stops",
                json={
                    "instrument_id": "NSE:RELIANCE",
                    "quantity": 10,
                    "stop_price_paise": 9_000,
                    "target_price_paise": 12_000,
                    "product": "MIS",
                },
                headers=headers,
            )
            assert stop.status_code == 200
            stop_body = stop.json()
            assert stop_body["status"] == "ACTIVE"
            listed = await client.get("/api/v1/paper/accounts/paper-main/stops")
            assert listed.status_code == 200
            assert listed.json()[0]["stop_id"] == stop_body["stop_id"]
            cancelled = await client.post(
                f"/api/v1/paper/accounts/paper-main/stops/{stop_body['stop_id']}/cancel",
                headers=headers,
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "CANCELLED"

    asyncio.run(check())
