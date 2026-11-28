from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bq.analysis.decision import (
    DataFreshness,
    OrderType,
    Product,
    Signal,
    TradePlan,
)
from bq.data.symbols import Symbol
from bq.data.vendors import (
    Bar,
    BarInterval,
    DataDomain,
    NoMarketDataError,
    Quote,
    VendorRouter,
)
from bq.infra.database import migrate
from bq.memory import (
    Episode,
    EpisodicMemoryStore,
    OutcomeResolver,
    ResolutionError,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def _store(tmp_path: Path) -> tuple[EpisodicMemoryStore, FrozenClock]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    return EpisodicMemoryStore(path, clock), clock


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


def _bar(event_at: datetime, *, high: str, low: str, close: str) -> Bar:
    return Bar(
        event_at=event_at,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1000,
    )


class StubVendor:
    name = "stub"
    is_realtime = False
    capabilities = frozenset({DataDomain.BARS_DAILY})

    def __init__(self, bars: tuple[Bar, ...]) -> None:
        self._bars = bars

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
        return self._bars


def _router(bars: tuple[Bar, ...]) -> VendorRouter:
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, StubVendor(bars))
    return router


def _resolver(
    store: EpisodicMemoryStore,
    bars: tuple[Bar, ...],
    *,
    clock: FrozenClock | None = None,
) -> OutcomeResolver:
    return OutcomeResolver(
        store,
        _router(bars),
        clock=clock or FrozenClock(),
        horizon_days=5,
    )


def _episode(
    store: EpisodicMemoryStore,
    *,
    as_of: datetime = NOW - timedelta(days=10),
    plan: TradePlan | None = None,
    action: str = "BUY",
    detail: dict[str, object] | None = None,
) -> Episode:
    return store.record_shadow(
        run_id="run-1",
        symbol="NSE:RELIANCE",
        as_of=as_of,
        source="analysis",
        decision_source="jev_native",
        model="jev-1.14.0",
        action=action,
        plan=plan,
        predicted_probability=0.8,
        detail=detail or {"last_close": "100.00"},
    )


def test_resolver_takes_the_target_first(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    episode = _episode(store, plan=_plan(NOW - timedelta(days=10)))
    bars = (
        _bar(NOW - timedelta(days=8), high="106", low="98", close="104"),
        _bar(NOW - timedelta(days=7), high="112", low="101", close="111"),
    )
    report = _resolver(store, bars, clock=clock).resolve_due()
    assert report.resolved == 1
    outcome = store.outcome(episode.episode_id)
    assert outcome.outcome == 1
    assert outcome.realized_r == pytest.approx(2.0)
    assert outcome.close_price_paise == 11200


def test_resolver_takes_the_stop_first(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    episode = _episode(store, plan=_plan(NOW - timedelta(days=10)))
    bars = (_bar(NOW - timedelta(days=8), high="101", low="94", close="96"),)
    report = _resolver(store, bars, clock=clock).resolve_due()
    assert report.resolved == 1
    outcome = store.outcome(episode.episode_id)
    assert outcome.outcome == 0
    assert outcome.realized_r == -1.0


def test_resolver_closes_at_the_horizon_or_forward_return(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    plan_episode = _episode(store, plan=_plan(NOW - timedelta(days=10)))
    hold_episode = _episode(
        store,
        action="HOLD",
        detail={"last_close": "100.00"},
    )
    bars = (_bar(NOW - timedelta(days=8), high="104", low="97", close="102"),)
    report = _resolver(store, bars, clock=clock).resolve_due()
    assert report.resolved == 2
    plan_outcome = store.outcome(plan_episode.episode_id)
    assert plan_outcome.outcome == 1
    assert plan_outcome.realized_r == pytest.approx(0.4)
    hold_outcome = store.outcome(hold_episode.episode_id)
    assert hold_outcome.outcome == 1


def test_resolver_skips_not_due_and_missing_bars(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    _episode(store, as_of=NOW - timedelta(days=1), plan=_plan(NOW - timedelta(days=1)))
    assert _resolver(store, (), clock=clock).resolve_due().examined == 0

    _episode(store, plan=_plan(NOW - timedelta(days=10)))
    report = _resolver(store, (), clock=clock).resolve_due()
    assert report.examined == 1
    assert report.skipped == 1
    assert report.resolved == 0
    assert "no bars" in report.notes[0]


def test_resolver_rejects_bad_horizon(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    with pytest.raises(ResolutionError, match="Horizon"):
        OutcomeResolver(store, _router(()), horizon_days=0)
