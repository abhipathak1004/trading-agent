"""The 24-claim scoped-evidence ablation (arXiv:2611.24845 §4.3) and boundaries."""

from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.research_lab.evidence import (
    REASON_BASIS,
    REASON_OK,
    REASON_UNIT,
    REASON_VALUE,
    REASON_WRONG_RUN,
    EvidenceClaim,
    EvidenceError,
    EvidenceLedger,
)
from bq.research_lab.store import ResearchRunStore, ResearchSessionStore

METRICS = ("sortino", "calmar", "turnover", "sharpe")
RUN_A = {"sortino": 1.2, "calmar": 0.8, "turnover": 0.35, "sharpe": 1.5}
RUN_B = {"sortino": 0.9, "calmar": 1.1, "turnover": 0.55, "sharpe": 0.7}
INVENTED = 999.0


def _ledger(tmp_path: Path) -> tuple[EvidenceLedger, str, str]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    session = ResearchSessionStore(path).create(title="ablation")
    runs = ResearchRunStore(path)
    run_a = runs.create(
        session_id=session.session_id, prompt="risk parity", model_config={}, params={}
    )
    run_b = runs.create(
        session_id=session.session_id, prompt="equal weight", model_config={}, params={}
    )
    ledger = EvidenceLedger(path)
    ledger.admit(
        run_a.run_id,
        "backtest",
        RUN_A,
        {
            "sortino": ("ratio", "annualized"),
            "calmar": ("ratio", "annualized"),
            "turnover": ("ratio", "point"),
            "sharpe": ("ratio", "annualized"),
        },
        source_ref="run_a/artifacts",
    )
    ledger.admit(
        run_b.run_id,
        "backtest",
        RUN_B,
        {
            "sortino": ("ratio", "annualized"),
            "calmar": ("ratio", "annualized"),
            "turnover": ("ratio", "point"),
            "sharpe": ("ratio", "annualized"),
        },
        source_ref="run_b/artifacts",
    )
    return ledger, run_a.run_id, run_b.run_id


def _basis(metric: str) -> str:
    return "point" if metric == "turnover" else "annualized"


def test_twenty_four_claims_reach_every_expected_decision(tmp_path: Path) -> None:
    ledger, run_a, run_b = _ledger(tmp_path)
    claims: list[EvidenceClaim] = []
    expected: list[tuple[bool, str]] = []
    for run_id, own, other in ((run_a, RUN_A, RUN_B), (run_b, RUN_B, RUN_A)):
        for metric in METRICS:
            claims.append(
                EvidenceClaim(run_id, metric, own[metric], "ratio", _basis(metric))
            )
            expected.append((True, REASON_OK))
            claims.append(
                EvidenceClaim(run_id, metric, other[metric], "ratio", _basis(metric))
            )
            expected.append((False, REASON_WRONG_RUN))
            claims.append(
                EvidenceClaim(run_id, metric, INVENTED, "ratio", _basis(metric))
            )
            expected.append((False, REASON_VALUE))
    assert len(claims) == 24

    result = ledger.validate_report("draft", claims)
    decisions = result["decisions"]
    assert len(decisions) == 24
    for decision, (accepted, reason) in zip(decisions, expected, strict=True):
        assert decision["accepted"] is accepted, decision
        assert decision["reason"] == reason, decision
    assert result["accepted"] == 8
    assert result["rejected"] == 16
    assert result["passed"] is False


def test_value_only_matching_would_have_accepted_the_borrowed_claims(
    tmp_path: Path,
) -> None:
    """The ablation's point: pooling values loses exactly the run identity."""
    ledger, run_a, run_b = _ledger(tmp_path)
    pooled = {*RUN_A.values(), *RUN_B.values()}
    borrowed = [
        EvidenceClaim(run_a, metric, RUN_B[metric], "ratio", _basis(metric))
        for metric in METRICS
    ]
    # Every borrowed value exists somewhere in pooled evidence...
    assert all(claim.value in pooled for claim in borrowed)
    # ...but a scoped check still rejects all of them.
    result = ledger.validate_report("draft", borrowed)
    assert result["accepted"] == 0


def test_unit_and_time_basis_mismatches_are_rejected(tmp_path: Path) -> None:
    ledger, run_a, _ = _ledger(tmp_path)
    wrong_unit = ledger.validate_report(
        "draft", [EvidenceClaim(run_a, "sortino", 1.2, "pct", "annualized")]
    )
    assert wrong_unit["decisions"][0]["reason"] == REASON_UNIT
    wrong_basis = ledger.validate_report(
        "draft", [EvidenceClaim(run_a, "sortino", 1.2, "ratio", "per_day")]
    )
    assert wrong_basis["decisions"][0]["reason"] == REASON_BASIS


def test_model_authored_output_is_never_admitted(tmp_path: Path) -> None:
    ledger, run_a, _ = _ledger(tmp_path)
    with pytest.raises(EvidenceError, match="not evidence"):
        ledger.admit(
            run_a,
            "write_file",
            {"sharpe": 2.0},
            {"sharpe": ("ratio", "annualized")},
            producer_kind="model",
        )


def test_a_metric_must_carry_a_unit_and_basis(tmp_path: Path) -> None:
    ledger, run_a, _ = _ledger(tmp_path)
    with pytest.raises(EvidenceError, match="unit and basis"):
        ledger.admit(run_a, "backtest", {"sharpe": 1.0}, {})


def test_non_finite_values_are_refused(tmp_path: Path) -> None:
    ledger, run_a, _ = _ledger(tmp_path)
    with pytest.raises(EvidenceError):
        ledger.admit(
            run_a, "backtest", {"sharpe": float("nan")}, {"sharpe": ("ratio", "point")}
        )
    with pytest.raises(EvidenceError):
        EvidenceClaim(run_a, "sharpe", float("inf"), "ratio")


def test_evidence_is_immutable(tmp_path: Path) -> None:
    import sqlite3

    ledger, run_a, _ = _ledger(tmp_path)
    with sqlite3.connect(tmp_path / "bq.db") as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE research_evidence SET value_num = 0")


def test_verified_claims_are_the_strip_survivors(tmp_path: Path) -> None:
    ledger, run_a, run_b = _ledger(tmp_path)
    result = ledger.validate_report(
        "draft",
        [
            EvidenceClaim(run_a, "sortino", 1.2, "ratio", "annualized"),
            EvidenceClaim(run_a, "sortino", INVENTED, "ratio", "annualized"),
        ],
    )
    survivors = ledger.verified_claims(result)
    assert len(survivors) == 1
    assert survivors[0]["value"] == 1.2
