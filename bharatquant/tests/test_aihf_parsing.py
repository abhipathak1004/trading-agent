"""Parsing of what aihf writes, and the structural ledger check.

Every fixture here is a shape aihf actually serialises (``SessionRecord``,
``FundBacktestResult``, ``ledger/<session>.json``), trimmed to the fields we
read. The point of these tests is that a malformed or hostile record is
rejected or bounded rather than rendered.
"""

import json

import pytest

from bq.aihf.config import resolve_paths
from bq.aihf.ledger import read_ledger, session_keys, structural_problem
from bq.aihf.parsing import (
    MAX_REASONING_CHARS,
    AiHfParseError,
    parse_backtest_detail,
    parse_backtest_summary,
    parse_clamp,
    parse_decision,
    parse_execution,
    parse_session,
    parse_session_document,
    parse_signal,
    trade_rows,
)


def _signal(**overrides):
    base = {
        "model_name": "buffett",
        "ticker": "AAPL",
        "date": "2025-01-02",
        "value": 0.7,
        "reasoning": "  durable moat, undervalued  ",
    }
    return {**base, **overrides}


def _decision(**overrides):
    base = {
        "as_of": "2025-01-02",
        "universe": ["AAPL", "MSFT"],
        "strategies": [
            {
                "name": "deep-value",
                "slice": 0.6,
                "signals": [_signal()],
                "final_contribution": {"AAPL": 0.3},
                "flat_reason": None,
            }
        ],
        "clamps": [
            {
                "limit": "max_position_pct",
                "ticker": "AAPL",
                "before": 0.4,
                "after": 0.25,
            }
        ],
        "target_weights": {"AAPL": 0.25},
        "final_weights": {"AAPL": 0.25},
        "drawdown": 0.03,
    }
    return {**base, **overrides}


def _session(**overrides):
    base = {
        "session": "2025-01-03",
        "fund": "example-fund",
        "universe": ["AAPL"],
        "nav": 101_000.0,
        "cash": 20_000.0,
        "positions": {"AAPL": 100},
        "marks": {"AAPL": 810.0},
        "benchmark": "SPY",
        "benchmark_close": 500.0,
        "rebalance": True,
        "prev_hash": "a" * 64,
        "hash": "b" * 64,
        "llm_model": "claude-opus-5-5",
        "executed": None,
        "decision": _decision(),
    }
    return {**base, **overrides}


def _backtest(**overrides):
    base = {
        "schema_version": 2,
        "fund": "example-fund",
        "start": "2025-01-02",
        "end": "2025-01-03",
        "rebalance": "weekly",
        "benchmark": "SPY",
        "universe": ["AAPL"],
        "capital": 100_000.0,
        "dates": ["2025-01-02", "2025-01-03"],
        "nav": [100_000.0, 101_000.0],
        "benchmark_nav": [100_000.0, 100_500.0],
        "metrics": {
            "total_return_pct": 0.01,
            "annualized_return_pct": 0.12,
            "sharpe_ratio": 1.5,
            "max_drawdown_pct": 0.02,
            "benchmark_return_pct": 0.005,
            "excess_return_pct": 0.005,
            "n_cycles": 1,
            "n_orders": 2,
            "traded_notional": 40_000.0,
            "trading_cost": 12.5,
        },
        "records": [_session()],
    }
    return {**base, **overrides}


# ---------------------------------------------------------------------------
# signals and decisions
# ---------------------------------------------------------------------------


def test_signal_is_clamped_and_reasoning_trimmed() -> None:
    signal = parse_signal(_signal(value=5.0))
    assert signal.value == 1.0
    assert parse_signal(_signal(value=-9.0)).value == -1.0
    assert signal.reasoning == "durable moat, undervalued"
    assert signal.side == "long"
    assert parse_signal(_signal(value=0.0)).side == "neutral"


def test_signal_reasoning_is_bounded() -> None:
    signal = parse_signal(_signal(reasoning="x" * 10_000))
    assert signal.reasoning is not None
    assert len(signal.reasoning) == MAX_REASONING_CHARS


def test_signal_without_a_number_is_rejected() -> None:
    with pytest.raises(AiHfParseError):
        parse_signal(_signal(value="high"))


def test_missing_reasoning_is_none_not_empty() -> None:
    assert parse_signal(_signal(reasoning=None)).reasoning is None
    assert parse_signal(_signal(reasoning="   ")).reasoning is None


def test_clamp_needs_numeric_before_and_after() -> None:
    clamp = parse_clamp({"limit": "max_gross_exposure", "before": 1.4, "after": 1.0})
    assert clamp.limit == "max_gross_exposure"
    assert clamp.ticker is None
    with pytest.raises(AiHfParseError):
        parse_clamp({"limit": "max_beta", "before": "n/a", "after": 1.0})


def test_decision_flags_follow_aihf_clamps() -> None:
    stopped = parse_decision(
        _decision(clamps=[{"limit": "max_drawdown_pct", "before": 0.21, "after": 0.0}])
    )
    assert stopped.is_stopped_out is True
    assert stopped.is_halved is False

    halved = parse_decision(
        _decision(
            clamps=[{"limit": "halve_drawdown_pct", "before": 0.11, "after": 0.05}]
        )
    )
    assert halved.is_halved is True
    assert halved.is_stopped_out is False


def test_decision_signal_count_counts_every_strategy() -> None:
    decision = parse_decision(
        _decision(
            strategies=[
                {"name": "a", "slice": 0.5, "signals": [_signal(), _signal()]},
                {"name": "b", "slice": 0.5, "signals": [_signal()]},
            ]
        )
    )
    assert decision.signal_count == 3


def test_execution_traded_notional_sums_fills() -> None:
    executed = parse_execution(
        {
            "as_of": "2025-01-02",
            "orders": [
                {"ticker": "AAPL", "side": "buy", "quantity": 10, "price": 800.0}
            ],
            "fills": [
                {"ticker": "AAPL", "side": "buy", "quantity": 10, "price": 805.0}
            ],
            "positions": {"AAPL": 10},
            "cash": 91_950.0,
            "nav": 100_000.0,
            "clamps": [],
        }
    )
    assert executed.traded_notional == 8_050.0
    assert executed.orders[0].notional == 8_000.0


def test_execution_rejects_a_bad_side() -> None:
    with pytest.raises(AiHfParseError):
        parse_execution(
            {
                "as_of": "2025-01-02",
                "orders": [
                    {"ticker": "AAPL", "side": "long", "quantity": 1, "price": 1}
                ],
                "fills": [],
            }
        )


def test_session_requires_a_name() -> None:
    with pytest.raises(AiHfParseError):
        parse_session(_session(session=""))


def test_session_document_must_be_json() -> None:
    with pytest.raises(AiHfParseError):
        parse_session_document("running example-fund over AAPL...")
    parsed = parse_session_document(json.dumps(_session()))
    assert parsed.session == "2025-01-03"
    assert parsed.raw["hash"] == "b" * 64


def test_session_without_a_decision_is_not_invented() -> None:
    parsed = parse_session(_session(decision=None, executed=None))
    assert parsed.decision is None
    assert parsed.executed is None
    assert parsed.drawdown is None


# ---------------------------------------------------------------------------
# backtests
# ---------------------------------------------------------------------------


def test_summary_reads_the_headline_metrics() -> None:
    summary = parse_backtest_summary(_backtest())
    assert summary.fund == "example-fund"
    assert summary.metrics.total_return_pct == 0.01
    assert summary.metrics.traded_notional == 40_000.0


def test_summary_rejects_a_non_backtest() -> None:
    with pytest.raises(AiHfParseError):
        parse_backtest_summary({"kind": "something else"})


def test_summary_rejects_missing_metrics() -> None:
    broken = _backtest()
    del broken["metrics"]["sharpe_ratio"]
    with pytest.raises(AiHfParseError, match="missing metrics"):
        parse_backtest_summary(broken)


def test_detail_requires_paired_series() -> None:
    broken = _backtest()
    broken["nav"] = [100_000.0]
    with pytest.raises(AiHfParseError, match="same length"):
        parse_backtest_detail(broken)


def test_trade_log_pairs_signal_and_execution_dates() -> None:
    second = _session(
        session="2025-01-04",
        executed={
            "as_of": "2025-01-03",
            "orders": [
                {"ticker": "AAPL", "side": "buy", "quantity": 5, "price": 800.0}
            ],
            "fills": [{"ticker": "AAPL", "side": "buy", "quantity": 5, "price": 804.0}],
            "positions": {"AAPL": 5},
            "cash": 95_980.0,
            "nav": 100_000.0,
            "clamps": [],
        },
        decision=None,
    )
    detail = parse_backtest_detail(_backtest(records=[_session(), second]))
    rows = trade_rows(detail)
    assert len(rows) == 1
    # Decided at session T, executed at T+1: the pair is never inferred.
    assert rows[0]["signal_date"] == "2025-01-03"
    assert rows[0]["execution_date"] == "2025-01-04"
    assert rows[0]["cost"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# the ledger chain
# ---------------------------------------------------------------------------


def _record(session: str, prev: str | None, own: str) -> dict:
    return {
        "session": session,
        "fund": "example-fund",
        "universe": ["AAPL"],
        "nav": 100_000.0,
        "cash": 0.0,
        "positions": {},
        "marks": {},
        "benchmark": "SPY",
        "benchmark_close": 1.0,
        "rebalance": False,
        "prev_hash": prev,
        "hash": own,
        "executed": None,
        "decision": None,
    }


def _write_chain(root, records) -> None:
    ledger = root / "ledger"
    ledger.mkdir(parents=True)
    for record in records:
        (ledger / f"{record['session']}.json").write_text(json.dumps(record))


def test_session_keys_find_daily_and_bar_records(tmp_path) -> None:
    ledger = tmp_path / "ledger"
    (ledger / "2025-01-03").mkdir(parents=True)
    (ledger / "2025-01-02.json").write_text("{}")
    (ledger / "2025-01-03" / "100500.json").write_text("{}")
    (ledger / "2025-01-03" / "checkpoint.json").write_text("{}")
    assert session_keys(ledger) == ["2025-01-02", "2025-01-03T10:05:00"]


def test_structural_problem_accepts_a_sound_chain() -> None:
    first = parse_session(_record("2025-01-02", None, "h1"))
    second = parse_session(_record("2025-01-03", "h1", "h2"))
    assert structural_problem([first, second]) is None


def test_structural_problem_catches_a_broken_link() -> None:
    first = parse_session(_record("2025-01-02", None, "h1"))
    second = parse_session(_record("2025-01-03", "not-h1", "h2"))
    assert structural_problem([first, second]) is not None


def test_structural_problem_catches_reordering_and_gaps() -> None:
    first = parse_session(_record("2025-01-03", None, "h1"))
    second = parse_session(_record("2025-01-02", "h1", "h2"))
    assert structural_problem([first, second]) is not None
    orphan = parse_session(_record("2025-01-02", "ghost", "h1"))
    assert structural_problem([orphan]) is not None


def test_read_ledger_reports_an_unreadable_chain(tmp_path) -> None:
    paths = resolve_paths(database_path=tmp_path / "bq.db")
    (paths.fund_dir("alpha") / "ledger").mkdir(parents=True)
    (paths.fund_dir("alpha") / "ledger" / "2025-01-02.json").write_text("{not json")
    view = read_ledger(paths, "alpha")
    assert view.chain_verified is False
    assert view.problem is not None


def test_read_ledger_pages_from_the_newest_end(tmp_path) -> None:
    paths = resolve_paths(database_path=tmp_path / "bq.db")
    _write_chain(
        paths.fund_dir("alpha"),
        [
            _record(
                "2025-01-0%d" % day,
                None if day == 2 else "h%d" % (day - 1),
                "h%d" % day,
            )
            for day in range(2, 8)
        ],
    )
    view = read_ledger(paths, "alpha", limit=2)
    assert [session.session for session in view.sessions] == [
        "2025-01-06",
        "2025-01-07",
    ]
    assert view.chain_verified is True


# ---------------------------------------------------------------------------
# error detail
# ---------------------------------------------------------------------------


def test_failure_detail_never_leaks_a_traceback() -> None:
    """A failure surfaced to the UI is a sentence, not a traceback.

    The engine runs in-process now, so the value is the exception's own message,
    capped. A traceback and a filesystem path are what the old subprocess
    scraper had to strip; the rule survives even though the mechanism changed.
    """
    from bq.aihf.runner import _failure_detail

    class EngineFailure(RuntimeError):
        pass

    detail = _failure_detail(
        EngineFailure("RELIANCE: no close on 2026-11-25; cannot value the book")
    )
    assert detail.startswith("RELIANCE:")
    assert "Traceback" not in detail
    assert len(detail) <= 400


def test_failure_detail_reports_a_silent_failure_as_its_type() -> None:
    from bq.aihf.runner import _failure_detail

    class NothingDue(RuntimeError):
        pass

    assert _failure_detail(NothingDue()) == "NothingDue"
