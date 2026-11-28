"""Scoped evidence ledger (E_t) and the fail-closed report gate.

Implements arXiv:2611.24845 §3.2.3 and the §4.3 ablation:

* **Admission.** Only deterministic producers (`engine`, `tool`) may enter E_t.
  Files or prose written by a model are `model` and are refused, so generated
  content can never validate its own numbers even when the bytes match an engine
  artifact.
* **Scoping.** A numeric claim is checked against the run it *names*. A value
  that exists only in another run is a wrong-run claim, not support — the thing
  value-only lookup cannot detect (§4.3, Figure 4).
* **Unit and basis.** A value can be correctly attributed and still carry the
  wrong measure. A claim must match both `unit` and `time_basis`, which is what
  separates per-day from annualised and decimal from percentage points.

The ledger is deliberately reusable: the Research Lab, the Bull/Bear debate and
the Super Agent all admit and validate through this one object.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from bq.core import Clock, SystemClock, open_connection, parse_timestamp, timestamp

UNITS: Final = frozenset({"ratio", "pct", "bp", "inr", "paise", "count", "bars"})
TIME_BASES: Final = frozenset({"point", "per_day", "per_bar", "annualized"})
ADMISSIBLE_PRODUCERS: Final = frozenset({"engine", "tool"})
ALL_PRODUCERS: Final = ADMISSIBLE_PRODUCERS | {"model"}

DEFAULT_ABS_TOLERANCE: Final = 1e-9
DEFAULT_REL_TOLERANCE: Final = 1e-9

REASON_OK: Final = "supported"
REASON_NO_EVIDENCE: Final = "no_evidence_for_run"
REASON_WRONG_RUN: Final = "value_only_in_another_run"
REASON_VALUE: Final = "value_mismatch"
REASON_UNIT: Final = "unit_mismatch"
REASON_BASIS: Final = "time_basis_mismatch"


class EvidenceError(ValueError):
    """The ledger refuses this operation."""


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    run_id: str
    metric_key: str
    value: float
    unit: str
    time_basis: str = "point"

    def __post_init__(self) -> None:
        if not self.run_id or not self.metric_key:
            raise EvidenceError("A claim needs a run id and a metric key.")
        if not math.isfinite(self.value):
            raise EvidenceError("A claim value must be finite.")
        if self.unit not in UNITS:
            raise EvidenceError(f"Unknown unit: {self.unit}.")
        if self.time_basis not in TIME_BASES:
            raise EvidenceError(f"Unknown time basis: {self.time_basis}.")


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    run_id: str
    seq: int
    tool_name: str
    metric_key: str
    value: float
    unit: str
    time_basis: str
    producer_kind: str
    source_ref: str
    admitted_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "tool_name": self.tool_name,
            "metric_key": self.metric_key,
            "value": self.value,
            "unit": self.unit,
            "time_basis": self.time_basis,
            "producer_kind": self.producer_kind,
            "source_ref": self.source_ref,
            "admitted_at_utc": timestamp(self.admitted_at),
        }


def decisions(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The per-claim decisions of a validation result, typed for callers."""
    raw = result.get("decisions")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _close(left: float, right: float, *, abs_tol: float, rel_tol: float) -> bool:
    return abs(left - right) <= max(abs_tol, rel_tol * abs(right))


class EvidenceLedger:
    """An append-only store of admitted numbers, scoped by run."""

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def admit(
        self,
        run_id: str,
        tool_name: str,
        payload: Mapping[str, float],
        unit_map: Mapping[str, tuple[str, str]],
        *,
        producer_kind: str = "engine",
        source_ref: str = "",
    ) -> tuple[EvidenceEntry, ...]:
        """Admit deterministic results into E_t.

        ``payload`` maps metric key to value; ``unit_map`` maps the same keys to
        ``(unit, time_basis)``. Every key must appear in both, so a number can
        never enter the ledger without the basis needed to interpret it.
        """
        if producer_kind not in ALL_PRODUCERS:
            raise EvidenceError(f"Unknown producer kind: {producer_kind}.")
        if producer_kind not in ADMISSIBLE_PRODUCERS:
            raise EvidenceError(
                "Model-authored output is not evidence: only engine and tool "
                "results may be admitted."
            )
        if not payload:
            raise EvidenceError("Nothing to admit.")
        missing = set(payload) - set(unit_map)
        if missing:
            raise EvidenceError(
                f"An admitted metric needs a unit and basis: {sorted(missing)}."
            )
        admitted_at = self.clock.now()
        rows: list[tuple[object, ...]] = []
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                current = connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM research_evidence WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                seq = int(str(current[0])) if current is not None else 0
                for metric_key in sorted(payload):
                    value = float(payload[metric_key])
                    if not math.isfinite(value):
                        raise EvidenceError("An admitted value must be finite.")
                    unit, basis = unit_map[metric_key]
                    if unit not in UNITS or basis not in TIME_BASES:
                        raise EvidenceError("An admitted unit or basis is unknown.")
                    seq += 1
                    rows.append(
                        (
                            run_id,
                            seq,
                            tool_name,
                            metric_key,
                            value,
                            unit,
                            basis,
                            producer_kind,
                            source_ref,
                            timestamp(admitted_at),
                        )
                    )
                connection.executemany(
                    "INSERT INTO research_evidence (run_id, seq, tool_name, "
                    "metric_key, value_num, unit, time_basis, producer_kind, "
                    "source_ref, admitted_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        return tuple(self.entries(run_id))

    def entries(self, run_id: str) -> tuple[EvidenceEntry, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT run_id, seq, tool_name, metric_key, value_num, unit, "
                "time_basis, producer_kind, source_ref, admitted_at_utc "
                "FROM research_evidence WHERE run_id = ? ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
        return tuple(
            EvidenceEntry(
                run_id=str(row[0]),
                seq=int(str(row[1])),
                tool_name=str(row[2]),
                metric_key=str(row[3]),
                value=float(str(row[4])),
                unit=str(row[5]),
                time_basis=str(row[6]),
                producer_kind=str(row[7]),
                source_ref=str(row[8]),
                admitted_at=parse_timestamp(str(row[9])),
            )
            for row in rows
        )

    def _value_elsewhere(
        self,
        metric_key: str,
        value: float,
        run_id: str,
        *,
        abs_tolerance: float,
        rel_tolerance: float,
    ) -> bool:
        """True when another run holds this exact value for the same metric.

        This is what separates a *borrowed* figure from an *invented* one: both
        fail a scoped check, but only the borrowed one would pass a check that
        pooled values without their run (§4.3, Figure 4).
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT run_id, value_num FROM research_evidence "
                "WHERE metric_key = ? AND run_id != ?",
                (metric_key, run_id),
            ).fetchall()
        return any(
            _close(
                float(str(row[1])), value, abs_tol=abs_tolerance, rel_tol=rel_tolerance
            )
            for row in rows
        )

    def validate_report(
        self,
        draft_text: str,
        claims: Sequence[EvidenceClaim],
        *,
        abs_tolerance: float = DEFAULT_ABS_TOLERANCE,
        rel_tolerance: float = DEFAULT_REL_TOLERANCE,
    ) -> dict[str, object]:
        """Check every numeric claim against E_t, scoped to the named run."""
        if not isinstance(draft_text, str):
            raise EvidenceError("A draft must be text.")
        decisions: list[dict[str, object]] = []
        for claim in claims:
            entries = [
                item
                for item in self.entries(claim.run_id)
                if item.metric_key == claim.metric_key
            ]
            borrowed = self._value_elsewhere(
                claim.metric_key,
                claim.value,
                claim.run_id,
                abs_tolerance=abs_tolerance,
                rel_tolerance=rel_tolerance,
            )
            if not entries:
                reason = REASON_WRONG_RUN if borrowed else REASON_NO_EVIDENCE
            else:
                matching_value = [
                    item
                    for item in entries
                    if _close(
                        item.value,
                        claim.value,
                        abs_tol=abs_tolerance,
                        rel_tol=rel_tolerance,
                    )
                ]
                if not matching_value:
                    reason = REASON_WRONG_RUN if borrowed else REASON_VALUE
                elif not any(item.unit == claim.unit for item in matching_value):
                    reason = REASON_UNIT
                elif not any(
                    item.time_basis == claim.time_basis for item in matching_value
                ):
                    reason = REASON_BASIS
                else:
                    reason = REASON_OK
            decisions.append(
                {
                    "run_id": claim.run_id,
                    "metric_key": claim.metric_key,
                    "value": claim.value,
                    "unit": claim.unit,
                    "time_basis": claim.time_basis,
                    "accepted": reason == REASON_OK,
                    "reason": reason,
                }
            )
        accepted = sum(1 for item in decisions if item["accepted"])
        return {
            "passed": accepted == len(decisions),
            "claims": len(decisions),
            "accepted": accepted,
            "rejected": len(decisions) - accepted,
            "decisions": decisions,
        }

    def verified_claims(
        self, result: Mapping[str, object]
    ) -> list[Mapping[str, object]]:
        """The claims that passed, for the "strip failing figures" boundary."""
        raw = result.get("decisions")
        if not isinstance(raw, list):
            return []
        return [
            item
            for item in raw
            if isinstance(item, Mapping) and item.get("accepted") is True
        ]


__all__ = [
    "ADMISSIBLE_PRODUCERS",
    "DEFAULT_ABS_TOLERANCE",
    "DEFAULT_REL_TOLERANCE",
    "EvidenceClaim",
    "EvidenceEntry",
    "EvidenceError",
    "EvidenceLedger",
    "REASON_BASIS",
    "REASON_NO_EVIDENCE",
    "REASON_OK",
    "REASON_UNIT",
    "REASON_VALUE",
    "REASON_WRONG_RUN",
    "TIME_BASES",
    "UNITS",
    "decisions",
]
