from datetime import UTC, datetime
from pathlib import Path

from bq.infra.database import migrate
from bq.risk.decisions import RiskDecisionStore
from bq.risk.policy import (
    ApprovalMode,
    ApprovalPolicy,
    RiskDecision,
    RiskDecisionState,
)


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def test_risk_and_approval_decisions_are_frozen(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = RiskDecisionStore(path, FrozenClock())
    risk = store.freeze_risk(
        decision_id="risk-1",
        opportunity_id="opportunity-1",
        decision=RiskDecision(
            state=RiskDecisionState.BLOCK,
            vetoes=("symbol_exposure", "insufficient_cash"),
        ),
    )
    approval = store.freeze_approval(
        decision_id="approval-1",
        opportunity_id="opportunity-1",
        policy=ApprovalPolicy(
            global_mode=ApprovalMode.AUTO,
            strategy_mode=ApprovalMode.APPROVE_ALL,
        ),
        approved=False,
    )
    assert store.get_risk("risk-1") == risk
    assert store.get_approval("approval-1") == approval
    assert approval.effective_mode is ApprovalMode.APPROVE_ALL
