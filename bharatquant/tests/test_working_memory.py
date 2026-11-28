import sqlite3
from contextlib import closing
from datetime import UTC, datetime
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
from bq.analysis.pipeline import AnalysisResult
from bq.analysis.technical import SubScore, TechnicalReport
from bq.compute.indicators import IndicatorSnapshot
from bq.infra.database import migrate
from bq.memory import (
    MissingWorkingMemory,
    WorkingMemoryConflict,
    WorkingMemoryStore,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _result(
    symbol: str = "NSE:RELIANCE", *, notes: tuple[str, ...] = ()
) -> AnalysisResult:
    indicator = IndicatorSnapshot(
        as_of=NOW,
        bar_count=120,
        close=100.0,
        sma_20=98.0,
        sma_50=95.0,
        ema_20=99.0,
        rsi_14=58.0,
        macd=None,
        bollinger=None,
        atr_14=2.0,
        vwap=97.0,
        adx_14=27.0,
        max_drawdown=0.1,
        volume=None,
        swings=None,
    )
    report = TechnicalReport(
        symbol=symbol,
        as_of=NOW,
        timeframe="swing",
        last_close=Decimal("100.00"),
        bar_count=120,
        trend=SubScore(0.8, ("trend",)),
        momentum=SubScore(0.5, ("momentum",)),
        volatility=SubScore(0.2, ("volatility",)),
        structure=SubScore(0.4, ("structure",)),
        indicators=indicator,
    )
    plan = TradePlan(
        signal=Signal.BUY,
        symbol=symbol,
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
        as_of=NOW,
    )
    return AnalysisResult(
        symbol=symbol,
        timeframe="swing",
        as_of=NOW,
        report=report,
        plan=plan,
        mode="RULES_ONLY",
        debate_rounds=0,
        vendor_daily="yahoo",
        vendor_quotes="yahoo",
        is_realtime=False,
        bull=None,
        bear=None,
        verdict=None,
        risk=None,
        vetoes=(),
        notes=notes,
        input_tokens=None,
        output_tokens=None,
        skipped_nodes=(),
    )


def _store(tmp_path: Path) -> WorkingMemoryStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return WorkingMemoryStore(path)


def test_working_memory_round_trips_and_is_append_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = _result()
    record = store.record(result, run_id="run-1", source="analysis")
    assert record.run_id == "run-1"
    assert record.symbol == "NSE:RELIANCE"
    assert record.schema_version == 1
    assert record.state["technical"]["bar_count"] == 120  # type: ignore[index]
    assert record.state["plan"]["entry_price"] == "100.00"  # type: ignore[index]

    duplicate = store.record(result, run_id="run-1", source="analysis")
    assert duplicate.state_hash == record.state_hash
    assert store.get("run-1") == (record,)

    with pytest.raises(WorkingMemoryConflict):
        store.record(_result(notes=("changed",)), run_id="run-1", source="analysis")

    with closing(sqlite3.connect(tmp_path / "bq.db")) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="cannot be updated"):
            connection.execute(
                "UPDATE working_memory SET source = 'strategy' WHERE run_id = 'run-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute("DELETE FROM working_memory WHERE run_id = 'run-1'")
        connection.rollback()


def test_strategy_keeps_one_row_per_symbol(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_result("NSE:RELIANCE"), run_id="strategy:book", source="strategy")
    store.record(_result("NSE:TCS"), run_id="strategy:book", source="strategy")
    records = store.get("strategy:book")
    assert [item.symbol for item in records] == ["NSE:RELIANCE", "NSE:TCS"]
    assert len({item.state_hash for item in records}) == 2


def test_missing_working_memory_is_typed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(MissingWorkingMemory):
        store.get("missing-run")
