import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from bq.analysis.decision import (
    DataFreshness,
    OrderType,
    Product,
    Signal,
    TradePlan,
)
from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
LIMITS = {
    "max_symbol_exposure_paise": 500_000,
    "max_open_positions": 5,
    "max_daily_loss_paise": 50_000,
    "max_weekly_loss_paise": 100_000,
    "max_monthly_loss_paise": 200_000,
    "max_order_rate_per_minute": 10,
}
ACCOUNT = {
    "account_id": "paper-main",
    "name": "Paper Main",
    "opening_balance_paise": 1_000_000,
}


def _app(tmp_path: Path):
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    auth.setup(PASSWORD)
    return create_app(database_path=path)


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def _plan(**overrides: object) -> TradePlan:
    values: dict[str, object] = {
        "signal": Signal.BUY,
        "symbol": "NSE:RELIANCE",
        "exchange": "NSE",
        "product": Product.CNC,
        "order_type": OrderType.LIMIT,
        "entry_price": Decimal("100.00"),
        "stop_loss": Decimal("95.00"),
        "target_price": Decimal("115.00"),
        "suggested_quantity": 10,
        "confidence": 0.6,
        "timeframe": "swing",
        "invalidation_reason": "Closes below 95.",
        "data_freshness": DataFreshness.DELAYED_YAHOO,
        "as_of": datetime.now(UTC),
    }
    values.update(overrides)
    return TradePlan(**values)  # type: ignore[arg-type]


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "account_id": "paper-main",
        "plan": _plan().to_dict(),
        "mode": "PAPER",
        "limits": LIMITS,
        "kill_switch": "NONE",
        "session_open_override": True,
    }
    body.update(overrides)
    return body


def test_execution_routes_require_auth_and_csrf(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (
                await client.post("/api/v1/execution/preview", json=_body())
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/paper/orders/order-1/fill",
                    json={
                        "quantity": 1,
                        "price_paise": 10_000,
                        "idempotency_key": "fill-1",
                    },
                )
            ).status_code == 401
            assert (
                await client.get("/api/v1/paper/accounts/paper-main/reconciliation")
            ).status_code == 401
            headers = await _signed_in(client)
            assert (
                await client.post(
                    "/api/v1/execution/preview",
                    json=_body(),
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            response = await client.post(
                "/api/v1/execution/preview", json=_body(), headers=headers
            )
            assert response.status_code == 422

    asyncio.run(check())


def test_account_create_list_and_book(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            created = await client.post(
                "/api/v1/paper/accounts", json=ACCOUNT, headers=headers
            )
            assert created.status_code == 200
            assert created.json()["balance_paise"] == "1000000"

            listed = await client.get("/api/v1/paper/accounts")
            assert listed.status_code == 200
            assert [item["account_id"] for item in listed.json()["accounts"]] == [
                "paper-main"
            ]

            book = await client.get("/api/v1/paper/accounts/paper-main/book")
            assert book.status_code == 200
            assert book.json()["positions"] == []
            assert book.json()["orders"] == []

    asyncio.run(check())


def test_preview_never_places_and_paper_place_is_idempotent(tmp_path: Path) -> None:
    app = _app(tmp_path)

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

            preview = await client.post(
                "/api/v1/execution/preview", json=_body(), headers=headers
            )
            assert preview.status_code == 200
            preview_body = preview.json()
            assert preview_body["state"] == "ALLOW"
            assert preview_body["order"] is None
            assert preview_body["context"]["session_source"] == "owner_override"
            assert preview_body["context"]["available_cash_paise"] == "1000000"

            first = await client.post(
                "/api/v1/execution/paper",
                json={**_body(), "client_order_id": "intent-1"},
                headers=headers,
            )
            assert first.status_code == 200
            first_body = first.json()
            assert first_body["state"] == "ALLOW"
            assert first_body["order"]["quantity"] == 10
            assert first_body["order"]["limit_price_paise"] == "10000"

            second = await client.post(
                "/api/v1/execution/paper",
                json={**_body(), "client_order_id": "intent-1"},
                headers=headers,
            )
            assert second.status_code == 200
            assert second.json()["order"]["order_id"] == first_body["order"]["order_id"]

            book = await client.get("/api/v1/paper/accounts/paper-main/book")
            assert len(book.json()["orders"]) == 1

    asyncio.run(check())


def test_disabled_modes_and_bad_plans_are_typed_errors(tmp_path: Path) -> None:
    app = _app(tmp_path)

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

            sandbox = await client.post(
                "/api/v1/execution/preview",
                json={**_body(), "mode": "KITE_SANDBOX"},
                headers=headers,
            )
            assert sandbox.status_code == 409
            assert "only PAPER" in sandbox.json()["detail"]

            bad_plan = await client.post(
                "/api/v1/execution/preview",
                json={**_body(), "plan": {"signal": "BUY"}},
                headers=headers,
            )
            assert bad_plan.status_code == 422

            missing_account = await client.post(
                "/api/v1/execution/preview",
                json={**_body(), "account_id": "missing"},
                headers=headers,
            )
            assert missing_account.status_code == 422

    asyncio.run(check())


def test_fill_partial_full_idempotent_and_reconcile(tmp_path: Path) -> None:
    app = _app(tmp_path)

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
                json={**_body(), "client_order_id": "intent-fill"},
                headers=headers,
            )
            assert placed.status_code == 200
            order_id = placed.json()["order"]["order_id"]

            partial = await client.post(
                f"/api/v1/paper/orders/{order_id}/fill",
                json={
                    "quantity": 4,
                    "price_paise": 10_000,
                    "idempotency_key": "fill-1",
                },
                headers=headers,
            )
            assert partial.status_code == 200
            assert partial.json()["order"]["status"] == "PARTIALLY_FILLED"
            assert partial.json()["order"]["filled_quantity"] == 4
            assert partial.json()["fill"]["quantity"] == 4
            assert partial.json()["fill"]["realized_pnl_paise"] is None
            fill_id = partial.json()["fill"]["fill_id"]

            duplicate = await client.post(
                f"/api/v1/paper/orders/{order_id}/fill",
                json={
                    "quantity": 4,
                    "price_paise": 10_000,
                    "idempotency_key": "fill-1",
                },
                headers=headers,
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["fill"]["fill_id"] == fill_id
            assert duplicate.json()["order"]["filled_quantity"] == 4

            book = await client.get("/api/v1/paper/accounts/paper-main/book")
            assert book.json()["account"]["balance_paise"] == "960000"
            assert book.json()["positions"] == [
                {"instrument_id": "NSE:RELIANCE", "quantity": 4}
            ]

            final = await client.post(
                f"/api/v1/paper/orders/{order_id}/fill",
                json={
                    "quantity": 6,
                    "price_paise": 10_000,
                    "idempotency_key": "fill-2",
                },
                headers=headers,
            )
            assert final.status_code == 200
            assert final.json()["order"]["status"] == "FILLED"
            assert final.json()["order"]["filled_quantity"] == 10
            assert final.json()["fill"]["realized_pnl_paise"] is None

            book = await client.get("/api/v1/paper/accounts/paper-main/book")
            assert book.json()["account"]["balance_paise"] == "900000"
            assert book.json()["positions"][0]["quantity"] == 10

            reconciled = await client.get(
                "/api/v1/paper/accounts/paper-main/reconciliation"
            )
            assert reconciled.status_code == 200
            assert reconciled.json() == {
                "account_id": "paper-main",
                "order_count": 1,
                "fill_count": 2,
                "ledger_debit_paise": "100000",
                "ledger_credit_paise": "0",
                "position_count": 1,
            }

            overfill = await client.post(
                f"/api/v1/paper/orders/{order_id}/fill",
                json={
                    "quantity": 1,
                    "price_paise": 10_000,
                    "idempotency_key": "fill-3",
                },
                headers=headers,
            )
            assert overfill.status_code == 422

    asyncio.run(check())


def test_fill_above_the_limit_is_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)

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
                json={**_body(), "client_order_id": "intent-limit"},
                headers=headers,
            )
            order_id = placed.json()["order"]["order_id"]
            response = await client.post(
                f"/api/v1/paper/orders/{order_id}/fill",
                json={
                    "quantity": 1,
                    "price_paise": 10_001,
                    "idempotency_key": "fill-above-limit",
                },
                headers=headers,
            )
            assert response.status_code == 422
            assert "limit" in response.json()["detail"]

    asyncio.run(check())


def test_cancel_rejects_filled_and_cancels_pending(tmp_path: Path) -> None:
    app = _app(tmp_path)

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

            filled_order = await client.post(
                "/api/v1/execution/paper",
                json={**_body(), "client_order_id": "cancel-filled"},
                headers=headers,
            )
            filled_id = filled_order.json()["order"]["order_id"]
            assert (
                await client.post(
                    f"/api/v1/paper/orders/{filled_id}/fill",
                    json={
                        "quantity": 10,
                        "price_paise": 10_000,
                        "idempotency_key": "cancel-fill",
                    },
                    headers=headers,
                )
            ).status_code == 200
            filled_cancel = await client.post(
                f"/api/v1/paper/orders/{filled_id}/cancel", headers=headers
            )
            assert filled_cancel.status_code == 409

            pending = await client.post(
                "/api/v1/execution/paper",
                json={**_body(), "client_order_id": "cancel-pending"},
                headers=headers,
            )
            pending_id = pending.json()["order"]["order_id"]
            cancelled = await client.post(
                f"/api/v1/paper/orders/{pending_id}/cancel", headers=headers
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "CANCELLED"
            assert (
                await client.post(
                    f"/api/v1/paper/orders/{pending_id}/fill",
                    json={
                        "quantity": 1,
                        "price_paise": 10_000,
                        "idempotency_key": "cancel-then-fill",
                    },
                    headers=headers,
                )
            ).status_code == 422

    asyncio.run(check())


def test_partial_and_full_position_exit_credits_cash(tmp_path: Path) -> None:
    app = _app(tmp_path)

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
                json={**_body(), "client_order_id": "exit-buy"},
                headers=headers,
            )
            order_id = placed.json()["order"]["order_id"]
            buy_fill = await client.post(
                f"/api/v1/paper/orders/{order_id}/fill",
                json={
                    "quantity": 10,
                    "price_paise": 10_000,
                    "idempotency_key": "exit-buy-fill",
                },
                headers=headers,
            )
            assert buy_fill.status_code == 200
            assert buy_fill.json()["fill"]["realized_pnl_paise"] is None

            exit_path = "/api/v1/paper/accounts/paper-main/positions/NSE:RELIANCE/exit"
            partial = await client.post(
                exit_path,
                json={
                    "quantity": 4,
                    "price_paise": 11_000,
                    "client_order_id": "exit-partial",
                },
                headers=headers,
            )
            assert partial.status_code == 200
            partial_order = partial.json()
            assert partial_order["side"] == "SELL"
            assert partial_order["status"] == "PENDING"
            assert partial_order["quantity"] == 4

            duplicate = await client.post(
                exit_path,
                json={
                    "quantity": 4,
                    "price_paise": 11_000,
                    "client_order_id": "exit-partial",
                },
                headers=headers,
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["order_id"] == partial_order["order_id"]

            partial_fill = await client.post(
                f"/api/v1/paper/orders/{partial_order['order_id']}/fill",
                json={
                    "quantity": 4,
                    "price_paise": 11_000,
                    "idempotency_key": "exit-partial-fill",
                },
                headers=headers,
            )
            assert partial_fill.status_code == 200
            assert partial_fill.json()["fill"]["realized_pnl_paise"] == "4000"
            duplicate_fill = await client.post(
                f"/api/v1/paper/orders/{partial_order['order_id']}/fill",
                json={
                    "quantity": 4,
                    "price_paise": 11_000,
                    "idempotency_key": "exit-partial-fill",
                },
                headers=headers,
            )
            assert duplicate_fill.status_code == 200
            assert duplicate_fill.json()["fill"]["realized_pnl_paise"] == "4000"
            book = await client.get("/api/v1/paper/accounts/paper-main/book")
            assert book.json()["positions"] == [
                {"instrument_id": "NSE:RELIANCE", "quantity": 6}
            ]
            assert book.json()["account"]["balance_paise"] == "944000"

            final = await client.post(
                exit_path,
                json={
                    "quantity": 6,
                    "price_paise": 11_000,
                    "client_order_id": "exit-final",
                },
                headers=headers,
            )
            final_order = final.json()
            final_fill = await client.post(
                f"/api/v1/paper/orders/{final_order['order_id']}/fill",
                json={
                    "quantity": 6,
                    "price_paise": 11_000,
                    "idempotency_key": "exit-final-fill",
                },
                headers=headers,
            )
            assert final_fill.status_code == 200
            assert final_fill.json()["fill"]["realized_pnl_paise"] == "6000"
            book = await client.get("/api/v1/paper/accounts/paper-main/book")
            assert book.json()["positions"] == []
            assert book.json()["account"]["balance_paise"] == "1010000"

            over = await client.post(
                exit_path,
                json={
                    "quantity": 1,
                    "price_paise": 11_000,
                    "client_order_id": "exit-too-much",
                },
                headers=headers,
            )
            assert over.status_code == 422
            assert "exceeds" in over.json()["detail"]

            reconciled = await client.get(
                "/api/v1/paper/accounts/paper-main/reconciliation"
            )
            assert reconciled.status_code == 200
            assert reconciled.json()["fill_count"] == 3
            assert reconciled.json()["ledger_debit_paise"] == "100000"
            assert reconciled.json()["ledger_credit_paise"] == "110000"
            assert reconciled.json()["position_count"] == 0

    asyncio.run(check())
