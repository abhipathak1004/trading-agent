from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq.fundamentals import (
    FundamentalError,
    FundamentalSnapshot,
    FundamentalSource,
    ValuationAssumptions,
    compute_ratios,
    dcf_valuation,
)
from bq.infra.database import migrate
from bq.news import NewsCategory, NewsItem
from bq.research import ResearchStore, ThesisHorizon
from bq.research_expansion import (
    NoteSource,
    ResearchExpansionError,
    ResearchExpansionStore,
)

NOW = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
KNOWN = NOW - timedelta(days=1)
PERIOD = NOW - timedelta(days=30)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _snapshot() -> FundamentalSnapshot:
    return FundamentalSnapshot(
        instrument_id="NSE:RELIANCE",
        source=FundamentalSource.XBRL,
        period_end=PERIOD,
        known_at=KNOWN,
        revenue_paise=10_000_000,
        ebitda_paise=2_000_000,
        net_income_paise=1_000_000,
        total_debt_paise=2_000_000,
        cash_paise=500_000,
        shares_outstanding=100_000,
        book_value_paise=5_000_000,
        fcf_paise=800_000,
    )


def test_notes_are_point_in_time_and_news_can_be_imported(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ResearchExpansionStore(path, FrozenClock())
    note = store.create_note(
        source_type=NoteSource.YOUTUBE,
        source_id="youtube-test",
        title="Management interview",
        body="Capital allocation stayed disciplined.",
        published_at=KNOWN,
        known_at=KNOWN,
        instrument_id="NSE:RELIANCE",
        url="https://youtube.com/watch?v=test",
    )
    assert note.source_type is NoteSource.YOUTUBE
    assert (
        store.notes(instrument_id="NSE:RELIANCE", as_of=NOW)[0].note_id == note.note_id
    )
    assert store.notes(as_of=KNOWN - timedelta(seconds=1)) == ()
    with pytest.raises(ResearchExpansionError, match="future"):
        store.create_note(
            source_type=NoteSource.OWNER,
            source_id="owner-test",
            title="Future",
            body="Not known yet",
            published_at=KNOWN,
            known_at=NOW + timedelta(days=1),
        )

    item = NewsItem(
        news_id="news-1",
        source_id="pulse.zerodha",
        url="https://example.com/story",
        title="Company wins order",
        summary="A large order was announced.",
        category=NewsCategory.CORPORATE,
        long_term_impact=True,
        published_at=KNOWN,
        ingested_at=KNOWN,
    )
    imported = store.note_from_news(item, instrument_id="NSE:RELIANCE")
    assert imported.source_type is NoteSource.NEWS
    assert imported.source_id == "pulse.zerodha"


def test_fundamental_ratios_and_dcf_are_deterministic() -> None:
    snapshot = _snapshot()
    ratios = compute_ratios(snapshot, price_paise=12_000)
    assert ratios.market_cap_paise == 1_200_000_000
    assert ratios.pe_bps is not None and ratios.pe_bps > 0
    assert ratios.roe_bps == 2_000
    assert ratios.debt_equity_bps == 4_000
    dcf = dcf_valuation(
        snapshot,
        price_paise=12_000,
        assumptions=ValuationAssumptions(growth_bps=1_000),
    )
    assert dcf.equity_value_paise > 0
    assert dcf.per_share_paise > 0
    assert dcf.upside_bps is not None
    with pytest.raises(FundamentalError, match="positive free cash flow"):
        dcf_valuation(replace(snapshot, fcf_paise=0))


def test_fundamentals_thesis_evidence_and_claims_form_a_dossier(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ResearchExpansionStore(path, FrozenClock())
    record = store.freeze_fundamentals(_snapshot())
    assert record.snapshot.content_hash
    assert (
        store.fundamentals("NSE:RELIANCE", as_of=NOW)[0].snapshot_id
        == record.snapshot_id
    )

    note = store.create_note(
        source_type=NoteSource.FILING,
        source_id="filing-source",
        title="Annual report",
        body="FCF improved.",
        published_at=KNOWN,
        known_at=KNOWN,
        instrument_id="NSE:RELIANCE",
    )
    ResearchStore(path, FrozenClock()).create_thesis(
        thesis_id="thesis-1",
        instrument_id="NSE:RELIANCE",
        horizon=ThesisHorizon.LONG,
    )
    event = store.add_thesis_event(
        thesis_id="thesis-1",
        kind="INVALIDATION",
        statement="FCF falls below maintenance capex.",
        note_id=note.note_id,
        occurred_at=NOW,
    )
    assert store.thesis_events("thesis-1")[0].event_hash == event.event_hash

    claim = store.create_claim(
        instrument_id="NSE:RELIANCE",
        statement="Deleveraging is on track.",
        note_id=note.note_id,
    )
    assert claim.status == "PENDING"
    reviewed = store.review_claim(claim.claim_id, decision="ACCEPTED", note="checked")
    assert reviewed.status == "ACCEPTED"
    with pytest.raises(ResearchExpansionError, match="already"):
        store.review_claim(claim.claim_id, decision="REJECTED")

    dossier = store.dossier("NSE:RELIANCE", as_of=NOW, price_paise=12_000)
    assert dossier["instrument_id"] == "NSE:RELIANCE"
    assert len(dossier["notes"]) == 1
    assert dossier["ratios"] is not None
    assert dossier["dcf"] is not None
    assert dossier["theses"][0]["events"][0]["kind"] == "INVALIDATION"
    assert dossier["claims"][0]["status"] == "ACCEPTED"
