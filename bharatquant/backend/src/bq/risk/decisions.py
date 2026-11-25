import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from bq.core import Clock, SystemClock
from bq.infra.audit import append_event
from bq.risk.policy import (
    ApprovalMode,
    ApprovalPolicy,
    RiskDecision,
)

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class RiskDecisionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredRiskDecision:
    decision_id: str
    opportunity_id: str | None
    occurred_at: datetime
    risk: RiskDecision
    decision_hash: str


@dataclass(frozen=True, slots=True)
class StoredApprovalDecision:
    decision_id: str
    opportunity_id: str | None
    occurred_at: datetime
    effective_mode: ApprovalMode
    approved: bool
    decision_hash: str


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise RiskDecisionError("Decision timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class RiskDecisionStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def freeze_risk(
        self,
        *,
        decision_id: str,
        opportunity_id: str | None,
        decision: RiskDecision,
    ) -> StoredRiskDecision:
        if (
            not isinstance(decision_id, str)
            or IDENTIFIER.fullmatch(decision_id) is None
        ):
            raise RiskDecisionError("Invalid risk decision identifier.")
        if not isinstance(decision, RiskDecision):
            raise RiskDecisionError("Risk decision is required.")
        occurred_at = _utc(self.clock.now())
        payload = {
            "decision_id": decision_id,
            "opportunity_id": opportunity_id,
            "occurred_at": _timestamp(occurred_at),
            "state": decision.state.value,
            "vetoes": list(decision.vetoes),
        }
        digest = _hash(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT decision_hash FROM risk_decisions WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != digest:
                        raise RiskDecisionError(
                            "Risk decision identity has conflicting content."
                        )
                    return self.get_risk(decision_id)
                connection.execute(
                    "INSERT INTO risk_decisions "
                    "(decision_id, opportunity_id, occurred_at, state, "
                    "vetoes_json, decision_hash) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        decision_id,
                        opportunity_id,
                        _timestamp(occurred_at),
                        decision.state.value,
                        json.dumps(list(decision.vetoes), sort_keys=True),
                        digest,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=occurred_at,
                    actor="owner",
                    action="risk.decision.recorded",
                    object_type="risk_decision",
                    object_id=decision_id,
                    payload={"decision_hash": digest},
                )
        return self.get_risk(decision_id)

    def freeze_approval(
        self,
        *,
        decision_id: str,
        opportunity_id: str | None,
        policy: ApprovalPolicy,
        approved: bool,
    ) -> StoredApprovalDecision:
        if (
            not isinstance(decision_id, str)
            or IDENTIFIER.fullmatch(decision_id) is None
        ):
            raise RiskDecisionError("Invalid approval decision identifier.")
        if not isinstance(policy, ApprovalPolicy):
            raise RiskDecisionError("Approval policy is required.")
        if not isinstance(approved, bool):
            raise RiskDecisionError("Approval result must be a boolean.")
        effective = policy.effective_mode()
        occurred_at = _utc(self.clock.now())
        payload = {
            "decision_id": decision_id,
            "opportunity_id": opportunity_id,
            "occurred_at": _timestamp(occurred_at),
            "effective_mode": effective.value,
            "approved": approved,
        }
        digest = _hash(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT decision_hash FROM approval_decisions "
                    "WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != digest:
                        raise RiskDecisionError(
                            "Approval decision identity has conflicting content."
                        )
                    return self.get_approval(decision_id)
                connection.execute(
                    "INSERT INTO approval_decisions "
                    "(decision_id, opportunity_id, occurred_at, effective_mode, "
                    "approved, decision_hash) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        decision_id,
                        opportunity_id,
                        _timestamp(occurred_at),
                        effective.value,
                        1 if approved else 0,
                        digest,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=occurred_at,
                    actor="owner",
                    action="approval.decision.recorded",
                    object_type="approval_decision",
                    object_id=decision_id,
                    payload={"decision_hash": digest},
                )
        return self.get_approval(decision_id)

    def get_risk(self, decision_id: str) -> StoredRiskDecision:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT decision_id, opportunity_id, occurred_at, state, vetoes_json, "
                "decision_hash FROM risk_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            raise RiskDecisionError("Risk decision not found.")
        from bq.risk.policy import RiskDecisionState

        return StoredRiskDecision(
            decision_id=str(row[0]),
            opportunity_id=str(row[1]) if row[1] is not None else None,
            occurred_at=_parse_timestamp(str(row[2])),
            risk=RiskDecision(
                state=RiskDecisionState(str(row[3])),
                vetoes=tuple(json.loads(str(row[4]))),
            ),
            decision_hash=str(row[5]),
        )

    def get_approval(self, decision_id: str) -> StoredApprovalDecision:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT decision_id, opportunity_id, occurred_at, effective_mode, "
                "approved, decision_hash FROM approval_decisions "
                "WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            raise RiskDecisionError("Approval decision not found.")
        return StoredApprovalDecision(
            decision_id=str(row[0]),
            opportunity_id=str(row[1]) if row[1] is not None else None,
            occurred_at=_parse_timestamp(str(row[2])),
            effective_mode=ApprovalMode(str(row[3])),
            approved=bool(row[4]),
            decision_hash=str(row[5]),
        )
