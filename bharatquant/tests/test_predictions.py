from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.predictions import PredictionError, PredictionStore


class FrozenClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def test_prediction_freeze_resolution_and_idempotency(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    start = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
    end = start + timedelta(hours=6)
    clock = FrozenClock(start - timedelta(minutes=30))
    store = PredictionStore(path, clock)

    frozen = store.freeze(
        prediction_id="nifty-up",
        window_start=start,
        window_end=end,
        probability=0.72,
        positive_threshold=0.5,
        prompt_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    duplicate = store.freeze(
        prediction_id="nifty-up",
        window_start=start,
        window_end=end,
        probability=0.72,
        positive_threshold=0.5,
        prompt_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    assert duplicate == frozen

    clock.instant = end + timedelta(minutes=1)
    outcome = store.resolve(
        "nifty-up",
        occurred=True,
        evidence_source_id="fixture",
        evidence_record_id="record-1",
        resolved_at=clock.instant,
    )
    assert outcome.occurred is True
    assert store.get_outcome("nifty-up") == outcome
    with pytest.raises(PredictionError, match="conflicting resolution"):
        store.resolve("nifty-up", occurred=False, resolved_at=clock.instant)


def test_prediction_rejects_late_freeze_and_premature_resolution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    start = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    clock = FrozenClock(start + timedelta(seconds=1))
    store = PredictionStore(path, clock)
    with pytest.raises(PredictionError, match="before"):
        store.freeze(
            prediction_id="late",
            window_start=start,
            window_end=end,
            probability=0.5,
        )

    clock.instant = start - timedelta(minutes=10)
    store.freeze(
        prediction_id="valid",
        window_start=start,
        window_end=end,
        probability=0.5,
    )
    with pytest.raises(PredictionError, match="before"):
        store.resolve("valid", occurred=True, resolved_at=start)
