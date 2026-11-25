import json
from pathlib import Path

import pytest

from bq.analysis.decision import TradePlanError, trade_plan_from_json
from bq.cli import main

PLAN = {
    "signal": "BUY",
    "symbol": "NSE:RELIANCE",
    "exchange": "NSE",
    "product": "CNC",
    "order_type": "LIMIT",
    "entry_price": "1351.00",
    "stop_loss": "1306.47",
    "target_price": "1417.80",
    "reward_risk": "1.50",
    "suggested_quantity": 224,
    "confidence": 0.4,
    "timeframe": "swing",
    "invalidation_reason": "Closes below swing support.",
    "data_freshness": "delayed_yfinance",
    "as_of": "2026-11-25T09:15:00Z",
}


# -- plan JSON round-trip ---------------------------------------------------


def test_a_valid_plan_round_trips() -> None:
    plan = trade_plan_from_json(PLAN)
    assert plan.symbol == "NSE:RELIANCE"
    assert plan.suggested_quantity == 224
    # Re-serialising and re-parsing is stable.
    assert trade_plan_from_json(plan.to_dict()) == plan


def test_a_hand_edited_quantity_is_still_shaped_by_the_contract() -> None:
    """Reconstruction re-validates; a bad stop is rejected, not repaired."""
    broken = dict(PLAN, stop_loss="1400.00")  # stop above entry
    with pytest.raises(TradePlanError):
        trade_plan_from_json(broken)


def test_a_non_limit_plan_is_rejected() -> None:
    with pytest.raises(TradePlanError):
        trade_plan_from_json(dict(PLAN, order_type="MARKET"))


def test_missing_and_malformed_fields_are_rejected() -> None:
    for bad in (
        {},
        {"signal": "BUY"},
        dict(PLAN, as_of="not-a-date"),
        dict(PLAN, signal="STRONG_BUY"),
        dict(PLAN, entry_price="free"),
        dict(PLAN, suggested_quantity=0),
        dict(PLAN, confidence=1.9),
    ):
        with pytest.raises(TradePlanError):
            trade_plan_from_json(bad)


def test_a_non_object_payload_is_rejected() -> None:
    with pytest.raises(TradePlanError):
        trade_plan_from_json([PLAN])


# -- CLI -------------------------------------------------------------------


def _init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Path:
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    assert main(["init"]) == 0
    # `init` prints to stdout; drain it so the command under test owns the buffer.
    capsys.readouterr()
    return tmp_path / "bq.db"


def test_paper_execute_reports_a_block_without_erroring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A veto is a decision, not a crash: the command reports it and exits 0."""
    db = _init(tmp_path, monkeypatch, capsys)
    from bq.core import CurrencyPaise
    from bq.paper_accounts import PaperAccountStore

    PaperAccountStore(db).create(
        account_id="paper-main",
        name="Paper-Main",
        opening_balance_paise=CurrencyPaise(50_000_000),
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN), "utf-8")
    rc = main(
        [
            "paper",
            "execute",
            str(plan_file),
            "--account-id",
            "paper-main",
            "--available-cash-paise",
            "50000000",
            "--open-positions",
            "0",
            "--symbol-exposure-paise",
            "0",
            "--daily-loss-paise",
            "0",
            "--weekly-loss-paise",
            "0",
            "--monthly-loss-paise",
            "0",
            "--session-open",
            "false",  # deterministic veto: session closed
            "--max-symbol-exposure-paise",
            "50000000",
            "--max-open-positions",
            "5",
            "--max-daily-loss-paise",
            "5000000",
            "--max-weekly-loss-paise",
            "10000000",
            "--max-monthly-loss-paise",
            "20000000",
            "--max-order-rate-per-minute",
            "10",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "BLOCK"
    assert "session_closed" in payload["vetoes"]
    assert payload["order"] is None


def test_paper_execute_needs_a_paper_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing account is an operator error, not a silent no-op."""
    _init(tmp_path, monkeypatch, capsys)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN), "utf-8")
    rc = main(
        [
            "paper",
            "execute",
            str(plan_file),
            "--account-id",
            "paper-main",
            "--available-cash-paise",
            "50000000",
            "--open-positions",
            "0",
            "--symbol-exposure-paise",
            "0",
            "--daily-loss-paise",
            "0",
            "--weekly-loss-paise",
            "0",
            "--monthly-loss-paise",
            "0",
            "--session-open",
            "true",
            "--max-symbol-exposure-paise",
            "50000000",
            "--max-open-positions",
            "5",
            "--max-daily-loss-paise",
            "5000000",
            "--max-weekly-loss-paise",
            "10000000",
            "--max-monthly-loss-paise",
            "20000000",
            "--max-order-rate-per-minute",
            "10",
        ]
    )
    assert rc == 1
    assert "account" in capsys.readouterr().err.lower()


def test_paper_execute_rejects_a_broken_plan_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path, monkeypatch, capsys)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(dict(PLAN, order_type="MARKET")), "utf-8")
    rc = main(
        [
            "paper",
            "execute",
            str(plan_file),
            "--account-id",
            "paper-main",
            "--available-cash-paise",
            "50000000",
            "--open-positions",
            "0",
            "--symbol-exposure-paise",
            "0",
            "--daily-loss-paise",
            "0",
            "--weekly-loss-paise",
            "0",
            "--monthly-loss-paise",
            "0",
            "--session-open",
            "true",
            "--max-symbol-exposure-paise",
            "50000000",
            "--max-open-positions",
            "5",
            "--max-daily-loss-paise",
            "5000000",
            "--max-weekly-loss-paise",
            "10000000",
            "--max-monthly-loss-paise",
            "20000000",
            "--max-order-rate-per-minute",
            "10",
        ]
    )
    assert rc == 1
    message = capsys.readouterr().err
    # The error must name the offending field, not just say "invalid".
    assert "order_type" in message


def test_analysis_never_places_an_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`analyze` writes no paper order; only `paper execute` can."""
    import sqlite3
    from contextlib import closing

    db = _init(tmp_path, monkeypatch, capsys)
    with closing(sqlite3.connect(db)) as connection:
        count = connection.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
    assert count == 0
