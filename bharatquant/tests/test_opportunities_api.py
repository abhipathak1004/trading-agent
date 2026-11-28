import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.confidence import (
    ConfidenceAssessment,
    ConfidenceDisposition,
    ConfidenceLevel,
)
from bq.infra.database import migrate
from bq.opportunities import (
    OpportunityHorizon,
    OpportunitySide,
    OpportunityStore,
)

PASSWORD = "synthetic owner password"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _seed(path: Path) -> str:
    store = OpportunityStore(path, FrozenClock())
    assessment = ConfidenceAssessment(
        probability=0.81,
        level=ConfidenceLevel.VERY_HIGH,
        disposition=ConfidenceDisposition.SUGGEST,
        confirming_families=4,
        base_rate_samples=200,
        live_outcomes=0,
        calibrated=False,
        ev_r_after_costs=1.4,
        vetoes=(),
    )
    opportunity = store.freeze(
        opportunity_id="opp-1",
        instrument_id="NSE:RELIANCE",
        horizon=OpportunityHorizon.SWING,
        side=OpportunitySide.BUY,
        assessment=assessment,
        run_id="run-1",
        source="analysis",
        setup_id="trend_continuation_v1",
        confidence={"level": "VERY_HIGH", "probability": 0.81},
        plan={"entry_price": "100.00", "stop_loss": "95.00", "target_price": "110.00"},
    )
    store.record_shadow(
        opportunity,
        as_of=NOW - timedelta(days=10),
        entry_price_paise=10_000,
        stop_loss_paise=9_500,
        target_price_paise=11_000,
        quantity=10,
        not_taken_reason="paper_not_executed",
        plan=opportunity.plan or {},
    )
    return opportunity.opportunity_id


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_opportunity_board_and_transition(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    _seed(path)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/opportunities")).status_code == 401
            headers = await _signed_in(client)
            response = await client.get("/api/v1/opportunities")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            item = response.json()["opportunities"][0]
            assert item["opportunity_id"] == "opp-1"
            assert item["level"] == "VERY_HIGH"
            assert item["lifecycle"] == "NEW"
            assert item["confidence"]["level"] == "VERY_HIGH"
            assert item["plan"]["entry_price"] == "100.00"

            assert (
                await client.post(
                    "/api/v1/opportunities/opp-1/transition",
                    json={"lifecycle": "WATCHING"},
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            transitioned = await client.post(
                "/api/v1/opportunities/opp-1/transition",
                json={"lifecycle": "WATCHING", "note": "watching"},
                headers=headers,
            )
            assert transitioned.status_code == 200
            assert transitioned.json()["lifecycle"] == "WATCHING"

            summary = await client.get("/api/v1/opportunities/shadow/summary")
            assert summary.status_code == 200
            assert summary.json()["suggested_count"] == 0
            assert summary.json()["verdict"] == "NEUTRAL"

            invalid = await client.post(
                "/api/v1/opportunities/opp-1/transition",
                json={"lifecycle": "TRADED"},
                headers=headers,
            )
            assert invalid.status_code == 422

    asyncio.run(check())
