import asyncio
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

PASSWORD = "synthetic owner password"
BASE = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)


class StubVendor:
    name = "stub-backtest"
    is_realtime = False
    capabilities = frozenset({DataDomain.BARS_DAILY})

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
        del symbol, interval, start, end
        return tuple(
            Bar(
                event_at=BASE + timedelta(days=index),
                open=Decimal(str(100 + max(0, index - 30))),
                high=Decimal(str(101 + max(0, index - 30))),
                low=Decimal(str(99 + max(0, index - 30))),
                close=Decimal(str(100.5 + max(0, index - 30))),
                volume=1_000,
            )
            for index in range(80)
        )


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_strategy_backtest_and_review_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, StubVendor())
    monkeypatch.setattr("bq.api.build_analysis_router", lambda *_args: router)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/strategies")).status_code == 401
            headers = await _signed_in(client)
            listed = await client.get("/api/v1/strategies")
            assert listed.status_code == 200
            assert listed.headers["cache-control"] == "no-store"
            strategies = listed.json()["strategies"]
            assert {item["strategy_id"] for item in strategies} == {
                "seed-breakout-20",
                "seed-momentum-20",
            }
            seed = next(
                item for item in strategies if item["strategy_id"] == "seed-momentum-20"
            )
            assert seed["is_seed"] is True

            body = {
                "start": (BASE + timedelta(days=25)).isoformat(),
                "end": (BASE + timedelta(days=79)).isoformat(),
                "engine": "FAST",
                "initial_capital_paise": 10_000_000,
            }
            created = await client.post(
                "/api/v1/strategies/seed-momentum-20/1/backtest",
                json=body,
                headers=headers,
            )
            assert created.status_code == 200
            run = created.json()
            assert run["engine"] == "FAST"
            assert int(run["metrics"]["trades"]) >= 1
            assert int(run["metrics"]["charges_paise"]) > 0
            assert run["review_decision"] is None

            runs = await client.get("/api/v1/backtests")
            assert runs.status_code == 200
            assert runs.json()["runs"][0]["run_id"] == run["run_id"]

            detail = await client.get(f"/api/v1/backtests/{run['run_id']}")
            assert detail.status_code == 200
            assert detail.json()["content_hash"] == run["content_hash"]

            rejected = await client.post(
                f"/api/v1/backtests/{run['run_id']}/review",
                json={"decision": "REJECTED", "note": "fails charge review"},
                headers=headers,
            )
            assert rejected.status_code == 200
            assert rejected.json()["review_decision"] == "REJECTED"
            assert rejected.json()["review_note"] == "fails charge review"

            bad = await client.post(
                "/api/v1/strategies/seed-momentum-20/1/backtest",
                json={**body, "start": "not-a-date"},
                headers=headers,
            )
            assert bad.status_code == 422

    asyncio.run(check())
