import sqlite3
from contextlib import closing
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
from bq.infra.database import migrate
from bq.memory import (
    Episode,
    EpisodeAction,
    EpisodicMemoryStore,
    MemoryError,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.instant = NOW

    def now(self) -> datetime:
        return self.instant


def _store(tmp_path: Path) -> tuple[EpisodicMemoryStore, MutableClock]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = MutableClock()
    return EpisodicMemoryStore(path, clock), clock


def _plan() -> TradePlan:
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
        as_of=NOW - timedelta(hours=1),
    )


def _record(
    store: EpisodicMemoryStore,
    *,
    symbol: str = "NSE:RELIANCE",
    probability: float | None = 0.8,
    decision_source: str = "jev_native",
    model: str = "jev-1.14.0",
) -> Episode:
    return store.record_shadow(
        run_id="run-1",
        symbol=symbol,
        as_of=NOW - timedelta(hours=1),
        source="analysis",
        decision_source=decision_source,
        model=model,
        action=EpisodeAction.BUY,
        plan=_plan(),
        predicted_probability=probability,
        regime="trend_up",
        detail={"strategy": "momentum"},
    )


def test_record_episode_copies_the_plan_and_is_idempotent(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    episode = _record(store)
    assert episode.symbol == "NSE:RELIANCE"
    assert episode.entry_price_paise == 10000
    assert episode.stop_loss_paise == 9500
    assert episode.target_price_paise == 11000
    assert episode.quantity == 10
    assert episode.predicted_probability == 0.8
    assert episode.detail == {"strategy": "momentum"}
    again = _record(store)
    assert again.episode_id == episode.episode_id
    assert len(store.episodes(limit=10)) == 1


def test_episodes_are_point_in_time(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    _record(store)
    cut = NOW - timedelta(hours=2)
    assert store.episodes(limit=10, as_of=cut) == ()
    assert len(store.episodes(limit=10, as_of=NOW)) == 1
    clock.instant = NOW + timedelta(days=1)
    assert len(store.episodes(limit=10, as_of=NOW + timedelta(hours=1))) == 1


def test_resolution_and_brier_are_bucketed_by_source_and_model(
    tmp_path: Path,
) -> None:
    store, clock = _store(tmp_path)
    first = _record(store)
    second = _record(
        store,
        symbol="NSE:TCS",
        probability=0.3,
        decision_source="llm_fallback",
        model="gemini-2.5-flash",
    )
    clock.instant = NOW + timedelta(hours=1)
    store.resolve(
        first.episode_id,
        resolved_at=NOW + timedelta(hours=1),
        outcome=1,
        realized_r=1.5,
        close_price_paise=11000,
    )
    store.resolve(
        second.episode_id,
        resolved_at=NOW + timedelta(hours=1),
        outcome=0,
        realized_r=-1.0,
        close_price_paise=9500,
    )
    buckets = store.calibration(as_of=NOW + timedelta(hours=2))
    assert [bucket.decision_source for bucket in buckets] == [
        "jev_native",
        "llm_fallback",
    ]
    assert buckets[0].summary.count == 1
    assert buckets[0].summary.mean_brier == pytest.approx(0.04)
    assert buckets[1].summary.mean_brier == pytest.approx(0.09)
    # Before resolution, no calibration is observable.
    assert store.calibration(as_of=NOW) == ()


def test_resolution_cannot_precede_the_episode(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    episode = _record(store)
    with pytest.raises(MemoryError, match="precede"):
        store.resolve(
            episode.episode_id,
            resolved_at=NOW - timedelta(hours=2),
            outcome=1,
            realized_r=0.0,
        )


def test_invalid_values_and_append_only_tables_are_rejected(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    with pytest.raises(MemoryError, match="probability"):
        _record(store, probability=1.5)
    episode = _record(store)
    with closing(sqlite3.connect(tmp_path / "bq.db")) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE episodic_memory SET model = 'x' WHERE episode_id = ?",
                (episode.episode_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM episodic_memory WHERE episode_id = ?",
                (episode.episode_id,),
            )
