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
    OpportunityLifecycle,
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


def test_opportunity_lifecycle_and_shadow_outcome(tmp_path: Path) -> None:
    from datetime import timedelta
    from decimal import Decimal

    from bq.data.symbols import Symbol
    from bq.data.vendors import (
        Bar,
        BarInterval,
        DataDomain,
        NoMarketDataError,
        Quote,
        VendorRouter,
    )

    class StubVendor:
        name = "stub"
        is_realtime = False
        capabilities = frozenset({DataDomain.BARS_DAILY})

        def quote(self, symbol: Symbol) -> Quote:
            raise NoMarketDataError(symbol.canonical)

        def bars(
            self,
            symbol: Symbol,
            *,
            interval: BarInterval,
            start: datetime,
            end: datetime,
        ) -> tuple[Bar, ...]:
            del symbol, interval, start, end
            return (
                Bar(
                    event_at=NOW - timedelta(days=8),
                    open=Decimal("104"),
                    high=Decimal("112"),
                    low=Decimal("101"),
                    close=Decimal("111"),
                    volume=1000,
                ),
            )

    NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    path = tmp_path / "bq.db"
    migrate(path, create=True)
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
        opportunity_id="lifecycle",
        instrument_id="NSE:RELIANCE",
        horizon=OpportunityHorizon.SWING,
        side=OpportunitySide.BUY,
        assessment=assessment,
        run_id="run-1",
        source="analysis",
        setup_id="trend_continuation_v1",
        confidence={"level": "VERY_HIGH"},
        plan={"entry_price": "100.00"},
    )
    assert opportunity.lifecycle is OpportunityLifecycle.NEW
    assert [event.lifecycle.value for event in store.events("lifecycle")] == ["NEW"]
    watched = store.transition(
        "lifecycle", OpportunityLifecycle.WATCHING, note="watching"
    )
    assert watched.lifecycle is OpportunityLifecycle.WATCHING
    triggered = store.transition("lifecycle", OpportunityLifecycle.TRIGGERED)
    assert triggered.lifecycle is OpportunityLifecycle.TRIGGERED
    traded = store.transition("lifecycle", OpportunityLifecycle.TRADED)
    assert traded.lifecycle is OpportunityLifecycle.TRADED
    closed = store.transition("lifecycle", OpportunityLifecycle.CLOSED)
    assert closed.lifecycle is OpportunityLifecycle.CLOSED
    with pytest.raises(OpportunityError, match="Cannot move"):
        store.transition("lifecycle", OpportunityLifecycle.WATCHING)

    shadow = store.record_shadow(
        opportunity,
        as_of=NOW - timedelta(days=10),
        entry_price_paise=10_000,
        stop_loss_paise=9_500,
        target_price_paise=11_000,
        quantity=10,
        not_taken_reason="paper_not_executed",
        plan={"entry_price": "100.00", "stop_loss": "95.00", "target_price": "110.00"},
    )
    assert shadow.suggested is True
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, StubVendor())
    report = store.resolve_shadows(router, as_of=NOW, max_trades=10)
    assert report["resolved"] >= 1
    outcomes = store.shadow_outcomes(shadow.shadow_id)
    assert any(item.horizon_days == 10 and item.outcome == 1 for item in outcomes)
