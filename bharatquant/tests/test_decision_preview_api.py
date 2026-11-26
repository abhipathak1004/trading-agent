import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from bq.analysis.technical import SubScore, TechnicalReport
from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.compute.indicators import IndicatorSnapshot
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _report() -> TechnicalReport:
    indicator = IndicatorSnapshot(
        as_of=NOW,
        bar_count=120,
        close=100.0,
        sma_20=98.0,
        sma_50=95.0,
        ema_20=99.0,
        rsi_14=55.0,
        macd=None,
        bollinger=None,
        atr_14=2.0,
        vwap=97.0,
        adx_14=27.0,
        max_drawdown=0.1,
        volume=None,
        swings=None,
    )
    return TechnicalReport(
        symbol="NSE:RELIANCE",
        as_of=NOW,
        timeframe="swing",
        last_close=Decimal("100.00"),
        bar_count=120,
        trend=SubScore(0.5, ("close above sma_50",)),
        momentum=SubScore(0.3, ("rsi above 50",)),
        volatility=SubScore(-0.2, ("atr elevated",)),
        structure=SubScore(0.4, ("support below",)),
        indicators=indicator,
    )


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


def test_decision_preview_requires_auth_and_csrf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    report = _report()
    monkeypatch.setattr(
        "bq.api._technical_report_for",
        lambda *_args, **_kwargs: (report, False, "yahoo", "yahoo", ()),
    )

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            body = {"symbol": "NSE:RELIANCE", "timeframe": "swing"}
            assert (
                await client.post("/api/v1/decision/preview", json=body)
            ).status_code == 401
            headers = await _signed_in(client)
            assert (
                await client.post(
                    "/api/v1/decision/preview",
                    json=body,
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            response = await client.post(
                "/api/v1/decision/preview", json=body, headers=headers
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"

    asyncio.run(check())


def test_decision_preview_returns_the_heuristic_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    report = _report()
    monkeypatch.setattr(
        "bq.api._technical_report_for",
        lambda *_args, **_kwargs: (report, False, "yahoo", "yahoo", ("note",)),
    )

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/decision/preview",
                json={"symbol": "NSE:RELIANCE", "timeframe": "swing"},
                headers=headers,
            )
            assert response.status_code == 200
            body = response.json()
            assert body["symbol"] == "NSE:RELIANCE"
            assert body["decision_preview"] == "heuristic"
            assert body["data_provenance"]["data_freshness"] == "delayed_yfinance"
            assert body["state"]["composite"] == 0.25
            assert [item["name"] for item in body["judgments"]] == [
                "setup_direction",
                "setup_quality",
                "worth_deep_research",
                "event_risk",
            ]
            assert body["route"]["decision_source"] == "heuristic"
            assert body["route"]["model"] == "heuristic"
            assert body["route"]["fallback_used"] is True
            assert body["screener"]["verdict"] in {"research", "skip"}
            assert body["notes"] == ["note"]

    asyncio.run(check())


def test_decision_preview_rejects_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    report = _report()
    monkeypatch.setattr(
        "bq.api._technical_report_for",
        lambda *_args, **_kwargs: (report, False, "yahoo", "yahoo", ()),
    )

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            response = await client.post(
                "/api/v1/decision/preview",
                json={"symbol": "NIFTY", "timeframe": "swing"},
                headers=headers,
            )
            assert response.status_code == 422

    asyncio.run(check())
