"""Independent paper-trade reconciliation from fills."""

import pytest

from bq.research_lab.reconcile import ReconcileError, paper_trade_reconcile

INITIAL = 2_000_000
FILLS = [
    {
        "symbol": "AAA",
        "side": "BUY",
        "quantity": 100,
        "price_paise": 10_000,
        "product": "CAS",
        "fees_paise": 1_187,
    }
]
CLEAN = {
    "cash_paise": 998_813,
    "fees_paise": 1_187,
    "realized_pnl_paise": 0,
    "unrealized_pnl_paise": 50_000,
    "nav_paise": 2_048_813,
    "positions": {"AAA": 100},
}


def test_clean_ledger_passes() -> None:
    result = paper_trade_reconcile(INITIAL, FILLS, {"AAA": 10_500}, CLEAN)
    assert result["passed"] is True
    assert result["discrepancies"] == []
    summary = result["computed_summary"]
    assert summary["cash_paise"] == 998_813
    assert summary["nav_paise"] == 2_048_813
    assert summary["unrealized_pnl_paise"] == 50_000


def test_fees_are_recomputed_when_not_supplied() -> None:
    fills = [{k: v for k, v in FILLS[0].items() if k != "fees_paise"}]
    result = paper_trade_reconcile(INITIAL, fills, {"AAA": 10_500}, CLEAN)
    # compute_charges is the single fee formula, so the total matches.
    assert result["computed_summary"]["fees_paise"] == 1_187
    assert result["passed"] is True


def test_one_rupee_drift_fails_with_the_exact_difference() -> None:
    reported = dict(CLEAN)
    reported["fees_paise"] = 1_287
    result = paper_trade_reconcile(INITIAL, FILLS, {"AAA": 10_500}, reported)
    assert result["passed"] is False
    (discrepancy,) = result["discrepancies"]
    assert discrepancy["field"] == "fees_paise"
    assert discrepancy["difference"] == 100
    assert discrepancy["computed"] == 1_187
    assert discrepancy["reported"] == 1_287


def test_tolerance_can_absorb_rounding_but_not_a_rupee() -> None:
    reported = dict(CLEAN)
    reported["fees_paise"] = 1_190
    assert (
        paper_trade_reconcile(
            INITIAL, FILLS, {"AAA": 10_500}, reported, tolerance_paise=5
        )["passed"]
        is True
    )
    assert (
        paper_trade_reconcile(
            INITIAL, FILLS, {"AAA": 10_500}, reported, tolerance_paise=1
        )["passed"]
        is False
    )


def test_position_drift_is_reported_per_symbol() -> None:
    reported = dict(CLEAN)
    reported["positions"] = {"AAA": 90}
    result = paper_trade_reconcile(INITIAL, FILLS, {"AAA": 10_500}, reported)
    assert result["passed"] is False
    fields = {item["field"] for item in result["discrepancies"]}
    assert fields == {"positions.AAA"}


def test_realised_pnl_on_a_round_trip() -> None:
    fills = [
        {
            "symbol": "AAA",
            "side": "BUY",
            "quantity": 100,
            "price_paise": 10_000,
            "fees_paise": 0,
        },
        {
            "symbol": "AAA",
            "side": "SELL",
            "quantity": 100,
            "price_paise": 11_000,
            "fees_paise": 0,
        },
    ]
    result = paper_trade_reconcile(INITIAL, fills, {}, {})
    summary = result["computed_summary"]
    assert summary["realized_pnl_paise"] == 100_000
    assert summary["positions"] == []
    assert summary["cash_paise"] == INITIAL + 100_000


def test_oversell_and_missing_mark_are_refused() -> None:
    with pytest.raises(ReconcileError, match="exceeds"):
        paper_trade_reconcile(
            INITIAL,
            [{"symbol": "AAA", "side": "SELL", "quantity": 10, "price_paise": 10_000}],
            {"AAA": 10_000},
            {},
        )
    with pytest.raises(ReconcileError, match="ending price"):
        paper_trade_reconcile(INITIAL, FILLS, {}, {})


def test_invalid_headers_are_refused() -> None:
    with pytest.raises(ReconcileError):
        paper_trade_reconcile(0, FILLS, {"AAA": 10_500}, {})
    with pytest.raises(ReconcileError):
        paper_trade_reconcile(INITIAL, FILLS, {"AAA": 10_500}, {}, tolerance_paise=-1)
