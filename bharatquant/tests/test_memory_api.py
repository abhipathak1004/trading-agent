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
from bq.memory import EpisodicMemoryStore

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
