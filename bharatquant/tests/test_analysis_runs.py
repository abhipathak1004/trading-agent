from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.analysis.run_settings import (
    AnalysisRun,
    AnalysisRunError,
    AnalysisRunStore,
)
from bq.infra.database import migrate

AS_OF = datetime(2026, 11, 25, 9, 15, tzinfo=UTC)
STARTED = datetime(2026, 11, 25, 9, 16, tzinfo=UTC)


class FrozenClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def _store(tmp_path: Path) -> AnalysisRunStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return AnalysisRunStore(
        path, FrozenClock(datetime(2026, 11, 25, 9, 20, tzinfo=UTC))
    )


def _record(store: AnalysisRunStore, **overrides: object) -> AnalysisRun:
    values: dict[str, object] = {
        "symbol": "NSE:RELIANCE",
        "timeframe": "swing",
        "as_of": AS_OF,
        "started_at": STARTED,
        "vendor_quotes": "yahoo",
        "vendor_daily": "yahoo",
        "is_realtime": False,
        "data_freshness": "delayed_yfinance",
        "bar_count": 200,
        "facts": ("Trend (+0.50): close above SMA20",),
        "composite": 0.42,
        "debate_attempted": True,
        "debate_rounds": 2,
        "mode": "FULL",
        "provider": "GEMINI",
        "model": "gemini-3.5-flash-lite",
        "input_tokens": 1200,
        "output_tokens": 340,
        "signal": "BUY",
        "plan": {"signal": "BUY", "entry_price": "1351.00"},
        "vetoes": (),
        "detail": {"report": {}, "notes": []},
    }
    values.update(overrides)
    return store.record(**values)  # type: ignore[arg-type]


def test_record_then_read_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _record(store)
    assert run.symbol == "NSE:RELIANCE"
    assert run.signal == "BUY"
    assert run.detail == {"report": {}, "notes": []}
    assert run.plan_sha256 is not None


def test_record_is_immutable(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    store = _store(tmp_path)
    run = _record(store)
    with closing(sqlite3.connect(store.path)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "UPDATE analysis_runs SET signal = 'SELL' WHERE run_id = ?",
                    (run.run_id,),
                )
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "DELETE FROM analysis_runs WHERE run_id = ?", (run.run_id,)
                )


def test_recent_returns_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, symbol="NSE:RELIANCE")
    _record(store, symbol="NSE:TCS")
    runs = store.recent(10)
    assert len(runs) == 2


def test_recent_can_filter_by_symbol(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record(store, symbol="NSE:RELIANCE")
    _record(store, symbol="NSE:TCS")
    runs = store.recent(10, symbol="NSE:TCS")
    assert len(runs) == 1
    assert runs[0].symbol == "NSE:TCS"


def test_get_unknown_run_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AnalysisRunError):
        store.get("does-not-exist")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeframe", "scalp"),
        ("data_freshness", "live"),
        ("mode", "PARTIAL"),
        ("signal", "STRONG_BUY"),
        ("bar_count", -1),
        ("debate_rounds", -1),
        ("input_tokens", -5),
    ],
)
def test_invalid_fields_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    store = _store(tmp_path)
    with pytest.raises(AnalysisRunError):
        _record(store, **{field: value})


def test_oversized_detail_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AnalysisRunError):
        _record(store, detail={"blob": "x" * 70_000})


def test_null_tokens_are_allowed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _record(store, input_tokens=None, output_tokens=None)
    assert run.input_tokens is None


def test_rules_only_run_records_no_plan_sha(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _record(store, plan=None, mode="RULES_ONLY", debate_rounds=0)
    assert run.plan_json is None
    assert run.plan_sha256 is None
    assert run.mode == "RULES_ONLY"


def test_facts_hash_changes_with_facts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _record(store, facts=("a",))
    second = _record(store, facts=("b",))
    assert first.facts_sha256 != second.facts_sha256


def test_run_writes_an_audit_event(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    store = _store(tmp_path)
    _record(store)
    with closing(sqlite3.connect(store.path)) as connection:
        rows = connection.execute(
            "SELECT action FROM audit_events WHERE action = 'analysis.run.recorded'"
        ).fetchall()
    assert len(rows) == 1


def test_plan_hash_is_stable_for_identical_plans(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _record(store, plan={"a": 1, "b": 2})
    second = _record(store, plan={"b": 2, "a": 1})
    assert first.plan_sha256 == second.plan_sha256
