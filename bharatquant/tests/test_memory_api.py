import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

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
from bq.memory import EpisodicMemoryStore, ResolutionReport, SemanticMemoryStore

PASSWORD = "synthetic owner password"


def _plan(as_of: datetime) -> TradePlan:
    return TradePlan(
        signal=Signal.BUY,
        symbol="NSE:RELIANCE",
        exchange="NSE",
        product=Product.CNC,
        order_type=OrderType.LIMIT,
        entry_price=Decimal("100.00"),
        stop_loss=Decimal("95.00"),
        target_price=Decimal("110.00"),
        suggested_quantity=10,
        confidence=0.7,
        timeframe="swing",
        invalidation_reason="close below support",
        data_freshness=DataFreshness.DELAYED_YAHOO,
        as_of=as_of,
    )


def test_memory_calibration_requires_auth_and_reports_buckets(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    now = datetime.now(UTC)
    store = EpisodicMemoryStore(path)
    episode = store.record_shadow(
        run_id="run-1",
        symbol="NSE:RELIANCE",
        as_of=now - timedelta(hours=1),
        source="analysis",
        decision_source="jev_native",
        model="jev-1.14.0",
        action="BUY",
        plan=_plan(now - timedelta(hours=1)),
        predicted_probability=0.8,
        regime="trend_up",
        detail={"source": "test"},
    )
    store.resolve(
        episode.episode_id,
        resolved_at=now,
        outcome=1,
        realized_r=1.5,
        close_price_paise=11000,
    )
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/memory/calibration")).status_code == 401
            auth.setup(PASSWORD)
            login = await client.post(
                "/api/v1/auth/login",
                json={"password": PASSWORD},
                headers={"Origin": ALLOWED_ORIGIN},
            )
            assert login.status_code == 200
            response = await client.get("/api/v1/memory/calibration")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert len(body["buckets"]) == 1
            bucket = body["buckets"][0]
            assert bucket["decision_source"] == "jev_native"
            assert bucket["model"] == "jev-1.14.0"
            assert bucket["count"] == 1
            assert bucket["mean_brier"] == pytest.approx(0.04)

    asyncio.run(check())


def test_semantic_search_api_is_authenticated_and_point_in_time(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    now = datetime.now(UTC)
    SemanticMemoryStore(path).ingest(
        symbol="NSE:RELIANCE",
        source_url="https://example.com/reliance",
        title="Reliance breakout",
        body="Reliance cleared resistance on strong volume.",
        published_at=now - timedelta(days=2),
        known_at=now - timedelta(days=2),
    )
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (
                await client.get("/api/v1/memory/semantic", params={"q": "breakout"})
            ).status_code == 401
            auth.setup(PASSWORD)
            login = await client.post(
                "/api/v1/auth/login",
                json={"password": PASSWORD},
                headers={"Origin": ALLOWED_ORIGIN},
            )
            assert login.status_code == 200
            response = await client.get(
                "/api/v1/memory/semantic", params={"q": "breakout"}
            )
            assert response.status_code == 200
            body = response.json()
            assert len(body["hits"]) == 1
            assert body["hits"][0]["symbol"] == "NSE:RELIANCE"
            assert "resistance" in body["hits"][0]["snippet"]
            empty = await client.get(
                "/api/v1/memory/semantic", params={"q": "nothing-matches"}
            )
            assert empty.json()["hits"] == []

    asyncio.run(check())


class FakeResolver:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def resolve_due(self) -> ResolutionReport:
        return ResolutionReport(
            examined=3, resolved=2, skipped=1, failed=0, notes=("NSE:TCS: no bars",)
        )


def test_memory_resolve_requires_csrf_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    app = create_app(database_path=path)
    monkeypatch.setattr("bq.api.OutcomeResolver", FakeResolver)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (
                await client.post("/api/v1/memory/resolve", json={})
            ).status_code == 401
            auth.setup(PASSWORD)
            login = await client.post(
                "/api/v1/auth/login",
                json={"password": PASSWORD},
                headers={"Origin": ALLOWED_ORIGIN},
            )
            csrf = login.json()["csrf_token"]
            assert (
                await client.post(
                    "/api/v1/memory/resolve",
                    json={},
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            response = await client.post(
                "/api/v1/memory/resolve",
                json={"horizon_days": 5, "max_episodes": 50},
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["examined"] == 3
            assert body["resolved"] == 2
            assert body["skipped"] == 1
            assert body["notes"] == ["NSE:TCS: no bars"]

    asyncio.run(check())


def test_playbook_routes_round_trip_and_require_csrf(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/memory/playbooks")).status_code == 401
            auth.setup(PASSWORD)
            login = await client.post(
                "/api/v1/auth/login",
                json={"password": PASSWORD},
                headers={"Origin": ALLOWED_ORIGIN},
            )
            csrf = login.json()["csrf_token"]
            listing = await client.get("/api/v1/memory/playbooks")
            assert listing.status_code == 200
            assert len(listing.json()["playbooks"]) == 3

            assert (
                await client.put(
                    "/api/v1/memory/playbooks/defensive",
                    json={"content": "CUSTOM GUIDANCE"},
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            saved = await client.put(
                "/api/v1/memory/playbooks/defensive",
                json={"content": "CUSTOM GUIDANCE"},
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert saved.status_code == 200
            assert saved.json()["content"] == "CUSTOM GUIDANCE"
            assert saved.json()["version"] == 1

            reset = await client.post(
                "/api/v1/memory/playbooks/defensive/reset",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert reset.status_code == 200
            assert reset.json()["is_default"] is True
            assert reset.json()["version"] == 2

            unknown = await client.put(
                "/api/v1/memory/playbooks/nope",
                json={"content": "x"},
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert unknown.status_code == 404
            empty = await client.put(
                "/api/v1/memory/playbooks/defensive",
                json={"content": ""},
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
            )
            assert empty.status_code == 422

    asyncio.run(check())
