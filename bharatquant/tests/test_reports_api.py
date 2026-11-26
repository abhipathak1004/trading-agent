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
NOW = datetime(2026, 11, 26, 2, 30, tzinfo=UTC)


class StubVendor:
    name = "stub-indices"
    is_realtime = False
    capabilities = frozenset({DataDomain.INDICES})

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
        base = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        price = Decimal("22000")
        return tuple(
            Bar(
                event_at=base + timedelta(days=index),
                open=price + Decimal(index),
                high=price + Decimal(index) + Decimal("30"),
                low=price + Decimal(index) - Decimal("30"),
                close=price + Decimal(index) + Decimal("10"),
                volume=100_000,
            )
            for index in range(330)
        )


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_reports_run_list_and_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    router = VendorRouter()
    router.register_vendor(DataDomain.INDICES, StubVendor())
    monkeypatch.setattr("bq.api.build_analysis_router", lambda *_args: router)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.post("/api/v1/reports/morning")).status_code == 401
            headers = await _signed_in(client)
            assert (
                await client.post(
                    "/api/v1/reports/morning",
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            created = await client.post("/api/v1/reports/morning", headers=headers)
            assert created.status_code == 200
            report = created.json()
            assert report["report_type"] == "morning"
            assert len(report["body"]["predictions"]) == 5
            assert report["content_hash"]
            assert report["file_path"] is not None

            listed = await client.get("/api/v1/reports")
            assert listed.status_code == 200
            assert listed.headers["cache-control"] == "no-store"
            assert listed.json()["reports"][0]["report_id"] == report["report_id"]

            detail = await client.get(f"/api/v1/reports/{report['report_id']}")
            assert detail.status_code == 200
            assert detail.json()["body_md"].startswith("# BharatQuant morning report")

            evening = await client.post("/api/v1/reports/evening", headers=headers)
            assert evening.status_code == 200
            assert evening.json()["report_type"] == "evening"

    asyncio.run(check())
