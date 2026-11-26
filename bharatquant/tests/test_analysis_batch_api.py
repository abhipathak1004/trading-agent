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
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {
        "Origin": ALLOWED_ORIGIN,
        "X-CSRF-Token": response.json().get("csrf_token", ""),
    }


def _body(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "symbols": "RELIANCE, TCS",
        "timeframe": "swing",
        "equity_paise": 10_000_000_00,
        "available_cash_paise": 5_000_000_00,
        "risk_basis_points": 100,
    }
    values.update(overrides)
    return values


def test_batch_requires_authentication(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            response = await client.post("/api/v1/analysis/batch", json=_body())
            assert response.status_code == 401

    import asyncio

    asyncio.run(check())


def test_batch_requires_csrf(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            await _signed_in(client)
            response = await client.post(
                "/api/v1/analysis/batch",
                json=_body(),
                headers={"Origin": ALLOWED_ORIGIN},  # no CSRF token
            )
            assert response.status_code == 403
            assert response.json()["detail"] == "Invalid CSRF token"

    import asyncio

    asyncio.run(check())


def test_batch_rejects_an_empty_symbol_list(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/analysis/batch",
                json=_body(symbols="  , , "),
                headers=headers,
            )
            assert response.status_code == 422

    import asyncio

    asyncio.run(check())


@pytest.mark.parametrize(
    "body",
    [
        _body(symbols=""),
        _body(equity_paise=0),
        _body(timeframe="scalp"),
        _body(risk_basis_points=20_000),
        _body(available_cash_paise=-1),
    ],
)
def test_batch_body_validation(tmp_path: Path, body: dict[str, object]) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/analysis/batch", json=body, headers=headers
            )
            assert response.status_code == 422

    import asyncio

    asyncio.run(check())


def test_batch_response_is_no_store(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            await _signed_in(client)
            # An unauthenticated-shaped request still exercises the middleware.
            response = await client.post("/api/v1/analysis/batch", json=_body())
            assert response.headers.get("cache-control") == "no-store"

    import asyncio

    asyncio.run(check())


def test_batch_is_bounded_at_ten_symbols(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    eleven = ", ".join(f"SYM{index}" for index in range(11))

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/analysis/batch",
                json=_body(symbols=eleven),
                headers=headers,
            )
            assert response.status_code == 422
            assert "At most 10" in response.json()["detail"]

    import asyncio

    asyncio.run(check())


def test_batch_payload_matches_the_single_run_shape(tmp_path: Path) -> None:
    """A batch item's analysis must carry the same keys the UI already parses.

    Regression: the pipeline's to_dict() has no `run_id` (only the run store
    mints one), so a batch item previously failed the UI's analysis contract with
    "Invalid analysis run". The API now attaches the stored id.
    """
    app, _ = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/analysis/batch",
                json=_body(symbols="NSE:NOTAREALTICKERXYZ"),
                headers=headers,
            )
            # Every symbol failed, which still returns a well-formed batch.
            assert response.status_code == 200
            payload = response.json()
            assert payload["requested"] == 1
            assert payload["failed"] == 1
            item = payload["items"][0]
            assert item["ok"] is False
            assert isinstance(item["error"], str)
            assert item["analysis"] is None

    import asyncio

    asyncio.run(check())


def test_batch_success_payload_is_contract_shaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful item carries the keys the UI contract requires.

    Data access is stubbed so this test asserts the response shape, not the
    network. Without the stubs it would depend on Yahoo being reachable.
    """
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from bq.data.symbols import Symbol
    from bq.data.vendors import (
        Bar,
        BarInterval,
        DataDomain,
        Quote,
        VendorRouter,
    )

    app, _ = _app(tmp_path)

    class StubVendor:
        name = "stub"
        is_realtime = False
        capabilities = frozenset({DataDomain.BARS_DAILY, DataDomain.BARS_INTRADAY})

        def quote(self, symbol: Symbol) -> Quote:
            from bq.data.vendors import NoMarketDataError

            raise NoMarketDataError(symbol.canonical)

        def bars(
            self,
            symbol: Symbol,
            *,
            interval: BarInterval,
            start: datetime,
            end: datetime,
        ) -> tuple[Bar, ...]:
            base = datetime(2026, 1, 1, tzinfo=UTC)
            out = []
            for index in range(200):
                price = Decimal(str(round(100 + index * 0.6, 2)))
                half = Decimal("0.6")
                out.append(
                    Bar(
                        event_at=base + timedelta(days=index),
                        open=price,
                        high=price + half,
                        low=price - half,
                        close=price,
                        volume=1000 + index,
                    )
                )
            return tuple(out)

    import bq.api as api_module

    def fake_router(database_path: Path, config: object) -> VendorRouter:
        router = VendorRouter()
        router.register_vendor(DataDomain.BARS_DAILY, StubVendor())
        router.register_vendor(DataDomain.BARS_INTRADAY, StubVendor())
        return router

    monkeypatch.setattr(api_module, "build_analysis_router", fake_router)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/analysis/batch",
                json=_body(symbols="NSE:RELIANCE", timeframe="swing"),
                headers=headers,
            )
            assert response.status_code == 200
            item = response.json()["items"][0]
            assert item["ok"] is True
            analysis = item["analysis"]
            # The fields the UI contract requires, all present.
            for key in (
                "run_id",
                "symbol",
                "timeframe",
                "mode",
                "as_of",
                "data_provenance",
                "report",
                "plan",
                "vetoes",
                "notes",
                "agents",
            ):
                assert key in analysis, f"missing {key}"
            assert isinstance(analysis["run_id"], str)
            assert analysis["run_id"]
            assert isinstance(analysis["agents"], list)
            assert analysis["agents"]

    import asyncio

    asyncio.run(check())
