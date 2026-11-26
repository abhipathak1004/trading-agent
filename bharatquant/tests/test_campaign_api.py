from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.campaign import CampaignNotFound
from bq.campaign_config import CampaignConfig
from bq.data.equity_master import EquityMasterStore
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"


class FakeCampaign:
    def to_dict(self) -> dict[str, object]:
        return {
            "campaign_id": "camp_20261126",
            "trade_date": "2026-11-26",
            "status": "READY",
            "items": [],
        }


class FakeService:
    calls: list[dict[str, object]] = []
    fail = False

    def __init__(self, path: Path, settings) -> None:
        self.config = CampaignConfig()

    def latest(self):
        return FakeCampaign()

    def get(self, campaign_id: str):
        if campaign_id == "missing":
            raise CampaignNotFound("Campaign 'missing' was not found.")
        return FakeCampaign()

    def research(self, **kwargs):
        if self.fail:
            raise ValueError("boom")
        FakeService.calls.append(kwargs)
        return FakeCampaign()

    def review(self, campaign_id: str):
        return {"campaign": self.get(campaign_id).to_dict(), "items": []}

    def cancel(self, campaign_id: str, *, as_of=None):
        return FakeCampaign()

    def refresh_master(self, *, as_of=None):
        return 3


class FakeScan:
    def to_dict(self) -> dict[str, object]:
        return {"session_open": True, "entries_triggered": 0}


class FakeEngine:
    def __init__(self, path: Path, settings) -> None:
        pass

    def scan(self, *, as_of=None):
        return FakeScan()


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_campaign_routes_require_auth_and_validate_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    monkeypatch.setattr("bq.api.CampaignService", FakeService)
    monkeypatch.setattr("bq.api.TriggerEngine", FakeEngine)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/campaign/latest")).status_code == 401
            headers = await _signed_in(client)

            latest = await client.get("/api/v1/campaign/latest")
            assert latest.status_code == 200
            assert latest.json()["campaign"]["campaign_id"] == "camp_20261126"
            assert latest.json()["config"]["top_n"] == 8

            saved = await client.put(
                "/api/v1/settings/campaign",
                json={"config": {"top_n": 6, "max_chase_bps": 80}},
                headers=headers,
            )
            assert saved.status_code == 200
            assert saved.json()["config"]["top_n"] == 6

            invalid = await client.put(
                "/api/v1/settings/campaign",
                json={"config": {"top_n": 20}},
                headers=headers,
            )
            assert invalid.status_code == 422

            researched = await client.post(
                "/api/v1/campaign/research",
                json={"force": True, "top_n": 6},
                headers=headers,
            )
            assert researched.status_code == 200
            assert researched.json()["campaign_id"] == "camp_20261126"
            assert FakeService.calls[-1]["force"] is True

            reviewed = await client.get("/api/v1/campaign/camp_20261126/review")
            assert reviewed.status_code == 200
            assert reviewed.json()["items"] == []
            assert (await client.get("/api/v1/campaign/missing")).status_code == 404

            scanned = await client.post(
                "/api/v1/campaign/camp_20261126/scan",
                json={},
                headers=headers,
            )
            assert scanned.status_code == 200
            assert scanned.json()["session_open"] is True

            refreshed = await client.post(
                "/api/v1/equity-master/refresh", headers=headers
            )
            assert refreshed.status_code == 200
            assert refreshed.json()["inserted"] == 3

            reset = await client.delete("/api/v1/settings/campaign", headers=headers)
            assert reset.status_code == 200
            assert reset.json()["config"]["top_n"] == 8

    import asyncio

    asyncio.run(check())


def test_equity_master_search_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    EquityMasterStore(path).refresh(
        (
            "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, "
            "MARKET LOT, ISIN NUMBER, FACE VALUE\n"
            "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
            "SUNPHARMA,Sun Pharmaceutical Industries Limited,EQ,08-JUL-1994,1,1,"
            "INE044A01036,1\n"
        ).encode("utf-8"),
        as_of=date(2026, 11, 26),
    )
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            found = await client.get("/api/v1/equity-master?q=sun")
            assert found.status_code == 200
            assert [row["symbol"] for row in found.json()["rows"]] == ["SUNPHARMA"]
            empty = await client.get("/api/v1/equity-master?q=")
            assert empty.json()["rows"] == []
            _ = headers

    import asyncio

    asyncio.run(check())
