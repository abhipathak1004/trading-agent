import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.data.symbols import Symbol
from bq.data.vendors import (
    Bar,
    BarInterval,
    DataDomain,
    NoMarketDataError,
    Quote,
    VendorRouter,
)
from bq.infra.database import migrate
from bq.settings import SettingsStore

PASSWORD = "synthetic owner password"
NOW = datetime.now(UTC)


class AcceptanceVendor:
    """Deterministic bars for equities and the three index regime inputs."""

    name = "acceptance"
    is_realtime = False
    capabilities = frozenset(
        {DataDomain.BARS_DAILY, DataDomain.BARS_INTRADAY, DataDomain.INDICES}
    )

    def quote(self, symbol: Symbol) -> Quote:
        raise NoMarketDataError(symbol.canonical)

    def bars(
        self,
        symbol: Symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> tuple[Bar, ...]:
        del interval, start, end
        if symbol.canonical == "INDEX:INDIA VIX":
            base, step, count = 12.0, 0.0, 240
        elif symbol.exchange.value == "INDEX":
            base, step, count = 22_000.0, 8.0, 240
        else:
            base, step, count = 100.0, 0.6, 240
        last = NOW - timedelta(minutes=30)
        bars: list[Bar] = []
        for index in range(count):
            value = Decimal(str(round(base + index * step, 2)))
            bars.append(
                Bar(
                    event_at=last - timedelta(days=count - 1 - index),
                    open=value,
                    high=value + Decimal("0.50"),
                    low=value - Decimal("0.50"),
                    close=value,
                    volume=10_000 + index * 10,
                )
            )
        return tuple(bars)


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    router = VendorRouter()
    for domain in (
        DataDomain.BARS_DAILY,
        DataDomain.BARS_INTRADAY,
        DataDomain.INDICES,
    ):
        router.register_vendor(domain, AcceptanceVendor())
    monkeypatch.setattr("bq.api.build_analysis_router", lambda *_args: router)
    # The monotonic fixture reaches final-gate conviction 3. The production
    # default (2, 3) abstains there; this test-owner setting admits 3 so the
    # acceptance can exercise the Strategy -> Trade seam end to end.
    store = SettingsStore(path)
    config = store.load()
    store.save_models(
        tiers=dict(config.tiers),
        agent_routing=dict(config.agent_routing),
        pipeline=config.pipeline,
        decision_route=config.decision_route,
        decision_policy=replace(config.decision_policy, abstain_score_band=(2, 2)),
    )
    return create_app(database_path=path)


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_acceptance_analysis_strategy_trade_fill_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path, monkeypatch)
    symbols = "NSE:RELIANCE, NSE:TCS, NSE:HDFCBANK"

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)

            batch = await client.post(
                "/api/v1/analysis/batch",
                json={
                    "symbols": symbols,
                    "timeframe": "swing",
                    "equity_paise": 10_000_000,
                    "available_cash_paise": 5_000_000,
                    "risk_basis_points": 100,
                },
                headers=headers,
            )
            assert batch.status_code == 200
            body = batch.json()
            assert body["succeeded"] == 3
            assert body["failed"] == 0
            for item in body["items"]:
                analysis = item["analysis"]
                assert analysis is not None
                assert analysis["report"]["bar_count"] >= 60
                nodes = {agent["node"] for agent in analysis["agents"]}
                assert {"evidence_verifier", "final_gate"} <= nodes
                assert analysis["mode"] == "RULES_ONLY"

            runs = await client.get("/api/v1/analysis/runs")
            assert runs.status_code == 200
            assert len(runs.json()["runs"]) == 3

            strategy = await client.post(
                "/api/v1/strategy/run",
                json={
                    "strategy": "Buy the strongest trend, cap the book at two.",
                    "symbols": symbols,
                    "timeframe": "swing",
                    "equity_paise": 10_000_000,
                    "available_cash_paise": 10_000_000,
                    "risk_basis_points": 100,
                    "max_positions": 2,
                },
                headers=headers,
            )
            assert strategy.status_code == 200
            strategy_body = strategy.json()
            assert strategy_body["decision"]["source"] == "rules"
            assert strategy_body["accepted"], "acceptance run produced no paper plan"
            assert len(strategy_body["accepted"]) <= 2

            account = await client.post(
                "/api/v1/paper/accounts",
                json={
                    "account_id": "acceptance",
                    "name": "Acceptance",
                    "opening_balance_paise": 100_000_000,
                },
                headers=headers,
            )
            assert account.status_code == 200

            plan = strategy_body["accepted"][0]["plan"]
            execution_body = {
                "account_id": "acceptance",
                "plan": plan,
                "mode": "PAPER",
                "limits": {
                    "max_symbol_exposure_paise": 100_000_000,
                    "max_open_positions": 5,
                    "max_daily_loss_paise": 10_000_000,
                    "max_weekly_loss_paise": 20_000_000,
                    "max_monthly_loss_paise": 30_000_000,
                    "max_order_rate_per_minute": 10,
                },
                "kill_switch": "NONE",
                "session_open_override": True,
            }
            preview = await client.post(
                "/api/v1/execution/preview", json=execution_body, headers=headers
            )
            assert preview.status_code == 200
            assert preview.json()["state"] == "ALLOW"
            assert preview.json()["order"] is None

            placed = await client.post(
                "/api/v1/execution/paper",
                json={**execution_body, "client_order_id": "acceptance-buy"},
                headers=headers,
            )
            assert placed.status_code == 200
            order = placed.json()["order"]
            assert order["status"] == "PENDING"

            filled = await client.post(
                f"/api/v1/paper/orders/{order['order_id']}/fill",
                json={
                    "quantity": order["quantity"],
                    "price_paise": int(order["limit_price_paise"]),
                    "idempotency_key": "acceptance-fill",
                },
                headers=headers,
            )
            assert filled.status_code == 200
            assert filled.json()["order"]["status"] == "FILLED"

            book = await client.get("/api/v1/paper/accounts/acceptance/book")
            assert book.status_code == 200
            book_body = book.json()
            assert book_body["positions"] == [
                {
                    "instrument_id": plan["symbol"],
                    "quantity": order["quantity"],
                }
            ]
            notional = int(order["limit_price_paise"]) * order["quantity"]
            assert book_body["account"]["balance_paise"] == str(100_000_000 - notional)

            reconciled = await client.get(
                "/api/v1/paper/accounts/acceptance/reconciliation"
            )
            assert reconciled.status_code == 200
            assert reconciled.json()["fill_count"] == 1
            assert reconciled.json()["position_count"] == 1

            calibration = await client.get("/api/v1/memory/calibration")
            assert calibration.status_code == 200

    asyncio.run(check())
