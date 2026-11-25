from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.research import (
    ResearchStore,
    ThesisHorizon,
    ThesisState,
)


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def test_filing_and_thesis_are_point_in_time_primitives(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ResearchStore(path, FrozenClock())
    published = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    ingested = datetime(2026, 11, 20, 11, 0, tzinfo=UTC)
    filing = store.freeze_filing(
        filing_id="filing-1",
        instrument_id="NSE:RELIANCE",
        filing_type="SHAREHOLDING",
        source_id="owner-reviewed-source",
        published_at=published,
        ingested_at=ingested,
    )
    thesis = store.create_thesis(
        thesis_id="thesis-1",
        instrument_id="NSE:RELIANCE",
        horizon=ThesisHorizon.LONG,
    )
    assert store.get_filing("filing-1") == filing
    assert store.get_thesis("thesis-1") == thesis
    assert thesis.state is ThesisState.DRAFT


def test_filing_rejects_ingestion_before_publication(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = ResearchStore(path, FrozenClock())
    with pytest.raises(ValueError, match="before publication"):
        store.freeze_filing(
            filing_id="filing-invalid",
            instrument_id="NSE:RELIANCE",
            filing_type="RESULTS",
            source_id="source",
            published_at=datetime(2026, 11, 20, 11, 0, tzinfo=UTC),
            ingested_at=datetime(2026, 11, 20, 10, 0, tzinfo=UTC),
        )
