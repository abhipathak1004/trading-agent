from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bq.backtest import BacktestEngineName, BacktestError, run_backtest
from bq.data.vendors import Bar
from bq.infra.database import migrate
from bq.strategies import StrategyError, StrategyKind, StrategyLifecycle
from bq.strategy_dsl import EntryRule, ExitRule, StrategyDefinition, from_dict
from bq.strategy_store import BacktestStore, StrategyStore, StrategyStoreError

BASE = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)


def _bars(*, close_after: int = 80) -> tuple[Bar, ...]:
    bars = []
    for index in range(close_after):
        price = 100 + max(0, index - 30)
        bars.append(
            Bar(
                event_at=BASE + timedelta(days=index),
                open=Decimal(str(price)),
                high=Decimal(str(price + 1)),
                low=Decimal(str(price - 1)),
                close=Decimal(str(price + 0.5)),
                volume=1_000,
            )
        )
    return tuple(bars)


def _definition(**overrides: object) -> StrategyDefinition:
    values: dict[str, object] = {
        "strategy_id": "test-momentum",
        "version": "1",
        "name": "Test momentum",
        "kind": StrategyKind.RULE,
        "timeframe": "swing",
        "universe": ("NSE:RELIANCE",),
        "entry_rule": EntryRule.SMA_CROSS,
        "exit_rule": ExitRule.SMA_CROSS_DOWN,
        "lookback": 20,
        "atr_period": 14,
    }
    values.update(overrides)
    return StrategyDefinition(**values)  # type: ignore[arg-type]


def test_dsl_roundtrip_and_strict_validation() -> None:
    definition = _definition()
    parsed = from_dict(definition.to_dict())
    assert parsed == definition
    assert parsed.content_hash == definition.content_hash
    with pytest.raises(StrategyError, match="unknown fields"):
        from_dict({**definition.to_dict(), "extra": 1})
    with pytest.raises(StrategyError, match="Target multiple"):
        _definition(target_atr_multiple=1.0)
    with pytest.raises(StrategyError, match="knowledge cutoff"):
        _definition(kind=StrategyKind.HYBRID)


def test_fast_and_event_engines_have_exact_parity() -> None:
    definition = _definition()
    bars = {"NSE:RELIANCE": _bars()}
    start = BASE + timedelta(days=25)
    end = BASE + timedelta(days=79)
    fast = run_backtest(
        definition,
        bars,
        start=start,
        end=end,
        initial_capital_paise=10_000_000,
        engine=BacktestEngineName.FAST,
    )
    event = run_backtest(
        definition,
        bars,
        start=start,
        end=end,
        initial_capital_paise=10_000_000,
        engine=BacktestEngineName.EVENT,
    )
    fast_payload = fast.to_dict()
    event_payload = event.to_dict()
    fast_payload.pop("engine")
    event_payload.pop("engine")
    assert fast_payload == event_payload
    assert fast.metrics.trades >= 1
    assert fast.metrics.charges_paise > 0
    assert (
        fast.metrics.net_pnl_paise
        == fast.metrics.gross_pnl_paise - fast.metrics.charges_paise
    )
    assert fast.metrics.final_capital_paise == 10_000_000 + fast.metrics.net_pnl_paise


def test_backtest_is_point_in_time_at_the_end_bound() -> None:
    definition = _definition()
    bars = {"NSE:RELIANCE": _bars()}
    start = BASE + timedelta(days=25)
    end = BASE + timedelta(days=45)
    first = run_backtest(
        definition,
        bars,
        start=start,
        end=end,
        initial_capital_paise=10_000_000,
    )
    later = {"NSE:RELIANCE": _bars(close_after=120)}
    second = run_backtest(
        definition,
        later,
        start=start,
        end=end,
        initial_capital_paise=10_000_000,
    )
    assert first.to_dict() == second.to_dict()
    with pytest.raises(BacktestError, match="timezone"):
        run_backtest(
            definition,
            bars,
            start=datetime(2026, 1, 1),
            end=end,
            initial_capital_paise=1,
        )


def test_strategy_versions_are_immutable_and_seeded(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = StrategyStore(path)
    store.ensure_seeds()
    seeds = store.list()
    assert {item.definition.strategy_id for item in seeds} == {
        "seed-breakout-20",
        "seed-momentum-20",
    }
    definition = _definition()
    saved = store.save(definition)
    assert saved.lifecycle is StrategyLifecycle.DRAFT
    assert store.save(definition).content_hash == saved.content_hash
    with pytest.raises(StrategyStoreError, match="conflicting"):
        store.save(replace(definition, name="Different"))
    with pytest.raises(StrategyStoreError, match="not found"):
        store.get("missing", "1")


def test_backtest_runs_and_reviews_are_append_only(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = StrategyStore(path)
    definition = _definition()
    store.save(definition)
    bars = {"NSE:RELIANCE": _bars()}
    start = BASE + timedelta(days=25)
    end = BASE + timedelta(days=79)
    result = run_backtest(
        definition,
        bars,
        start=start,
        end=end,
        initial_capital_paise=10_000_000,
    )
    runs = BacktestStore(path)
    recorded = runs.record(result)
    assert recorded.review_decision is None
    assert runs.get(recorded.run_id).content_hash == recorded.content_hash
    review = runs.review(recorded.run_id, decision="ACCEPTED", note="parity checked")
    assert review.decision == "ACCEPTED"
    assert runs.get(recorded.run_id).review_decision == "ACCEPTED"
    with pytest.raises(StrategyStoreError, match="ACCEPTED or REJECTED"):
        runs.review(recorded.run_id, decision="MAYBE", note="")
