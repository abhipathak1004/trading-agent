from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.confidence import (
    ConfidenceAssessment,
    ConfidenceDisposition,
    ConfidenceLevel,
)
from bq.infra.database import migrate
from bq.opportunities import (
    OpportunityError,
    OpportunityHorizon,
    OpportunitySide,
    OpportunityStore,
)


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def test_opportunity_freezes_suggested_and_watch_candidates(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = OpportunityStore(path, FrozenClock())
    assessment = ConfidenceAssessment(
        probability=0.72,
        level=ConfidenceLevel.HIGH,
        disposition=ConfidenceDisposition.SUGGEST,
        confirming_families=4,
        base_rate_samples=200,
        live_outcomes=60,
        calibrated=True,
        ev_r_after_costs=0.35,
        vetoes=(),
    )
    opportunity = store.freeze(
        opportunity_id="reliance-breakout",
        instrument_id="NSE:RELIANCE",
        horizon=OpportunityHorizon.SWING,
        side=OpportunitySide.BUY,
        assessment=assessment,
    )
    assert store.get(opportunity.opportunity_id) == opportunity

    watch = ConfidenceAssessment(
        probability=0.63,
        level=ConfidenceLevel.MODERATE,
        disposition=ConfidenceDisposition.WATCH,
        confirming_families=4,
        base_rate_samples=120,
        live_outcomes=30,
        calibrated=False,
        ev_r_after_costs=0.31,
        vetoes=(),
    )
    shadow = store.freeze(
        opportunity_id="infy-watch",
        instrument_id="NSE:INFY",
        horizon=OpportunityHorizon.SHORT_TERM,
        side=OpportunitySide.BUY,
        assessment=watch,
    )
    assert shadow.assessment.disposition is ConfidenceDisposition.WATCH


def test_opportunity_rejects_conflicting_same_id(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = OpportunityStore(path, FrozenClock())
    assessment = ConfidenceAssessment(
        probability=0.72,
        level=ConfidenceLevel.HIGH,
        disposition=ConfidenceDisposition.SUGGEST,
        confirming_families=4,
        base_rate_samples=200,
        live_outcomes=60,
        calibrated=True,
        ev_r_after_costs=0.35,
        vetoes=(),
    )
    store.freeze(
        opportunity_id="same",
        instrument_id="NSE:AAA",
        horizon=OpportunityHorizon.SWING,
        side=OpportunitySide.BUY,
        assessment=assessment,
    )
    with pytest.raises(OpportunityError, match="conflicting"):
        store.freeze(
            opportunity_id="same",
            instrument_id="NSE:BBB",
            horizon=OpportunityHorizon.SWING,
            side=OpportunitySide.BUY,
            assessment=assessment,
        )
