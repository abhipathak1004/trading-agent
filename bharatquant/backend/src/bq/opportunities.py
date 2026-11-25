import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from bq.confidence import (
    ConfidenceAssessment,
    ConfidenceDisposition,
    ConfidenceLevel,
)
from bq.core import Clock, SystemClock
from bq.infra.audit import append_event

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class OpportunityHorizon(StrEnum):
    INTRADAY = "INTRADAY"
    SHORT_TERM = "SHORT_TERM"
    SWING = "SWING"
    MEDIUM = "MEDIUM"
    LONG = "LONG"


class OpportunitySide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OpportunityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Opportunity:
    opportunity_id: str
    instrument_id: str
    horizon: OpportunityHorizon
    side: OpportunitySide
    created_at: datetime
    assessment: ConfidenceAssessment
    assessment_hash: str


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise OpportunityError("Opportunity timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class OpportunityStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def freeze(
        self,
        *,
        opportunity_id: str,
        instrument_id: str,
        horizon: OpportunityHorizon,
        side: OpportunitySide,
        assessment: ConfidenceAssessment,
    ) -> Opportunity:
        if (
            not isinstance(opportunity_id, str)
            or IDENTIFIER.fullmatch(opportunity_id) is None
        ):
            raise OpportunityError("Invalid opportunity identifier.")
        if (
            not isinstance(instrument_id, str)
            or IDENTIFIER.fullmatch(instrument_id) is None
        ):
            raise OpportunityError("Invalid instrument identifier.")
        if not isinstance(horizon, OpportunityHorizon):
            raise OpportunityError("Invalid opportunity horizon.")
        if not isinstance(side, OpportunitySide):
            raise OpportunityError("Invalid opportunity side.")
        if not isinstance(assessment, ConfidenceAssessment):
            raise OpportunityError("A confidence assessment is required.")

        created_at = _utc(self.clock.now())
        payload = {
            "opportunity_id": opportunity_id,
            "instrument_id": instrument_id,
            "horizon": horizon.value,
            "side": side.value,
            "created_at": _timestamp(created_at),
            "probability": assessment.probability,
            "disposition": assessment.disposition.value,
            "level": assessment.level.value,
            "calibrated": assessment.calibrated,
            "ev_r_after_costs": assessment.ev_r_after_costs,
            "vetoes": list(assessment.vetoes),
        }
        assessment_hash = sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                existing = connection.execute(
                    "SELECT assessment_hash FROM opportunities "
                    "WHERE opportunity_id = ?",
                    (opportunity_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != assessment_hash:
                        raise OpportunityError(
                            "Opportunity identity has conflicting content."
                        )
                    return self.get(opportunity_id)
                connection.execute(
                    "INSERT INTO opportunities "
                    "(opportunity_id, instrument_id, horizon, side, created_at, "
                    "probability, disposition, level, calibrated, ev_r, assessment_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        opportunity_id,
                        instrument_id,
                        horizon.value,
                        side.value,
                        _timestamp(created_at),
                        repr(assessment.probability),
                        assessment.disposition.value,
                        assessment.level.value,
                        1 if assessment.calibrated else 0,
                        repr(assessment.ev_r_after_costs),
                        assessment_hash,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=created_at,
                    actor="owner",
                    action="opportunity.frozen",
                    object_type="opportunity",
                    object_id=opportunity_id,
                    payload={"assessment_hash": assessment_hash},
                )
        return self.get(opportunity_id)

    def get(self, opportunity_id: str) -> Opportunity:
        if (
            not isinstance(opportunity_id, str)
            or IDENTIFIER.fullmatch(opportunity_id) is None
        ):
            raise OpportunityError("Invalid opportunity identifier.")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT opportunity_id, instrument_id, horizon, side, created_at, "
                "probability, disposition, level, calibrated, ev_r, assessment_hash "
                "FROM opportunities WHERE opportunity_id = ?",
                (opportunity_id,),
            ).fetchone()
        if row is None:
            raise OpportunityError("Opportunity not found.")
        assessment = ConfidenceAssessment(
            probability=float(row[5]),
            level=ConfidenceLevel(str(row[7])),
            disposition=ConfidenceDisposition(str(row[6])),
            confirming_families=0,
            base_rate_samples=0,
            live_outcomes=0,
            calibrated=bool(row[8]),
            ev_r_after_costs=float(row[9]),
            vetoes=(),
        )
        return Opportunity(
            opportunity_id=str(row[0]),
            instrument_id=str(row[1]),
            horizon=OpportunityHorizon(str(row[2])),
            side=OpportunitySide(str(row[3])),
            created_at=_parse_timestamp(str(row[4])),
            assessment=assessment,
            assessment_hash=str(row[10]),
        )
