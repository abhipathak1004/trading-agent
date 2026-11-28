import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
NOW = datetime.now(UTC)
KNOWN = (NOW - timedelta(days=1)).isoformat()
PERIOD = (NOW - timedelta(days=30)).isoformat()


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_research_notes_dossier_thesis_and_claim_review(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    app = create_app(database_path=path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/research/notes")).status_code == 401
            headers = await _signed_in(client)
            note = await client.post(
                "/api/v1/research/notes",
                json={
                    "instrument_id": "NSE:RELIANCE",
                    "source_type": "YOUTUBE",
                    "source_id": "youtube-test",
                    "url": "https://youtube.com/watch?v=test",
                    "title": "Management interview",
                    "body": "Capital allocation stayed disciplined.",
                    "published_at": KNOWN,
                    "known_at": KNOWN,
                },
                headers=headers,
            )
            assert note.status_code == 200
            note_body = note.json()
            assert note_body["source_type"] == "YOUTUBE"

            notes = await client.get(
                "/api/v1/research/notes?instrument_id=NSE:RELIANCE"
            )
            assert notes.status_code == 200
            assert notes.headers["cache-control"] == "no-store"
            assert notes.json()["notes"][0]["note_id"] == note_body["note_id"]

            snapshot = await client.post(
                "/api/v1/research/fundamentals",
                json={
                    "instrument_id": "NSE:RELIANCE",
                    "source": "XBRL",
                    "period_end": PERIOD,
                    "known_at": KNOWN,
                    "revenue_paise": 10_000_000,
                    "ebitda_paise": 2_000_000,
                    "net_income_paise": 1_000_000,
                    "total_debt_paise": 2_000_000,
                    "cash_paise": 500_000,
                    "shares_outstanding": 100_000,
                    "book_value_paise": 5_000_000,
                    "fcf_paise": 800_000,
                },
                headers=headers,
            )
            assert snapshot.status_code == 200
            assert snapshot.json()["source"] == "XBRL"

            dossier = await client.get(
                "/api/v1/research/instruments/NSE:RELIANCE/dossier?price_paise=12000"
            )
            assert dossier.status_code == 200
            dossier_body = dossier.json()
            assert dossier_body["ratios"] is not None
            assert dossier_body["dcf"] is not None

            thesis = await client.post(
                "/api/v1/research/theses",
                json={
                    "thesis_id": "thesis-api",
                    "instrument_id": "NSE:RELIANCE",
                    "horizon": "LONG",
                },
                headers=headers,
            )
            assert thesis.status_code == 200
            event = await client.post(
                "/api/v1/research/theses/thesis-api/events",
                json={
                    "kind": "INVALIDATION",
                    "statement": "FCF falls below maintenance capex.",
                    "note_id": note_body["note_id"],
                },
                headers=headers,
            )
            assert event.status_code == 200
            events = await client.get("/api/v1/research/theses/thesis-api/events")
            assert events.status_code == 200
            assert events.json()[0]["kind"] == "INVALIDATION"

            claim = await client.post(
                "/api/v1/research/claims",
                json={
                    "instrument_id": "NSE:RELIANCE",
                    "statement": "Deleveraging is on track.",
                    "note_id": note_body["note_id"],
                },
                headers=headers,
            )
            assert claim.status_code == 200
            claim_id = claim.json()["claim_id"]
            reviewed = await client.post(
                f"/api/v1/research/claims/{claim_id}/review",
                json={"decision": "ACCEPTED", "note": "checked"},
                headers=headers,
            )
            assert reviewed.status_code == 200
            assert reviewed.json()["status"] == "ACCEPTED"

            updated_dossier = await client.get(
                "/api/v1/research/instruments/NSE:RELIANCE/dossier?price_paise=12000"
            )
            assert updated_dossier.json()["claims"][0]["status"] == "ACCEPTED"
            assert (
                updated_dossier.json()["theses"][0]["events"][0]["kind"]
                == "INVALIDATION"
            )

    asyncio.run(check())
