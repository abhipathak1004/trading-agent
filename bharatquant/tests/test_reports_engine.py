import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

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
from bq.reports.engine import ReportEngine
from bq.reports.store import ReportStore

AS_OF = datetime(2026, 11, 26, 2, 30, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return AS_OF


class StubVendor:
    name = "stub-indices"
    is_realtime = False
    capabilities = frozenset({DataDomain.INDICES})

    def __init__(self) -> None:
        self.bars_data: tuple[Bar, ...] = ()

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
        if not self.bars_data:
            base = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
            price = Decimal("22000")
            self.bars_data = tuple(
                Bar(
                    event_at=base + timedelta(days=index),
                    open=price + Decimal(index),
                    high=price + Decimal(index) + Decimal("30"),
                    low=price + Decimal(index) - Decimal("30"),
                    close=price + Decimal(index) + Decimal("10"),
                    volume=100_000,
                )
                for index in range(330)
            )
        return self.bars_data


def _engine(tmp_path: Path) -> ReportEngine:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    router = VendorRouter()
    router.register_vendor(DataDomain.INDICES, StubVendor())
    return ReportEngine(path, router, clock=FrozenClock())


def test_morning_freezes_predictions_and_evening_resolves_them(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    morning = engine.morning(as_of=AS_OF)
    assert morning.report_type == "morning"
    assert morning.status == "ON_TIME"
    predictions = morning.body["predictions"]
    assert isinstance(predictions, list) and len(predictions) == 5
    assert {item["kind"] for item in predictions} == {
        "DIRECTION",
        "RANGE",
        "GAP",
        "VOLATILITY",
        "STANCE",
    }
    assert morning.body_md.startswith("# BharatQuant morning report")
    assert morning.file_path is not None
    assert Path(morning.file_path).exists()

    evening = engine.evening(as_of=AS_OF + timedelta(days=7))
    assert evening.report_type == "evening"
    assert evening.body["examined"] == 5
    assert evening.body["resolved"] == 5
    assert len(ReportStore(tmp_path / "bq.db").list()) == 2
    outcomes = evening.body["outcomes"]
    assert isinstance(outcomes, list) and len(outcomes) == 5
    scores = evening.body["scores"]
    assert isinstance(scores, dict) and scores["count"] == 5

    with sqlite3.connect(tmp_path / "bq.db") as connection:
        with pytest.raises(sqlite3.IntegrityError, match="reports are immutable"):
            connection.execute(
                "UPDATE reports SET title = 'rewritten' WHERE report_id = ?",
                (morning.report_id,),
            )
