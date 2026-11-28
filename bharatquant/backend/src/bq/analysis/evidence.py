"""Evidence verification for specialist claims.

The deterministic first pass checks that every cited evidence id exists in the
point-in-time news store. When a decision model is configured, a second Noul pass
asks whether the retrieved evidence actually supports each claim. A Noul pass can
only lower confidence: a claim that fails the citation check is never sent to the
model, and a model outage falls back to the citation result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from bq.analysis.briefs import Claim, SpecialistBrief
from bq.decision.battery import EVIDENCE, questions_for
from bq.decision.state import DecisionState
from bq.decision.types import DecisionOutcome
from bq.news import NewsItem, NewsStore

MAX_MODEL_CLAIMS = 20
MIN_NOUL_PROBABILITY = 0.60


class EvidenceError(ValueError):
    pass


class EvidenceRouterLike(Protocol):
    def decide(
        self,
        agent_id: str,
        *,
        state: DecisionState | Mapping[str, object],
        battery: str,
        task_id: str = "",
        claims: Sequence[str] = (),
    ) -> DecisionOutcome: ...


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    briefs: dict[str, SpecialistBrief]
    verified_claims: int
    unverified_claims: int
    missing_evidence_ids: tuple[str, ...]
    decision_source: str = "citation"
    model: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "verified_claims": self.verified_claims,
            "unverified_claims": self.unverified_claims,
            "missing_evidence_ids": list(self.missing_evidence_ids),
            "decision_source": self.decision_source,
            "model": self.model,
        }


class EvidenceVerifier:
    def __init__(
        self,
        store: NewsStore,
        *,
        router: EvidenceRouterLike | None = None,
        min_probability: float = MIN_NOUL_PROBABILITY,
    ) -> None:
        if not 0.0 <= min_probability <= 1.0:
            raise EvidenceError("The evidence probability threshold is out of range.")
        self.store = store
        self.router = router
        self.min_probability = min_probability

    def verify(
        self,
        briefs: Mapping[str, SpecialistBrief],
        *,
        as_of: datetime,
    ) -> EvidenceResult:
        if as_of.utcoffset() is None:
            raise EvidenceError("Evidence verification needs a timezone-aware as_of.")
        items = self.store.recent(limit=500, as_of=as_of)
        known = {item.news_id: item for item in items}
        flags: dict[tuple[str, int], bool] = {}
        missing: set[str] = set()
        candidates: list[tuple[str, int, Claim]] = []

        for agent, brief in briefs.items():
            for index, claim in enumerate(brief.claims):
                if not claim.evidence_ids:
                    flags[(agent, index)] = False
                    continue
                absent = tuple(
                    value for value in claim.evidence_ids if value not in known
                )
                if absent:
                    missing.update(absent)
                    flags[(agent, index)] = False
                    continue
                flags[(agent, index)] = True
                if len(candidates) < MAX_MODEL_CLAIMS:
                    candidates.append((agent, index, claim))

        decision_source = "citation"
        model: str | None = None
        if self.router is not None and candidates:
            try:
                result, decision_source, model = self._model_pass(
                    self.router, candidates, known, as_of=as_of
                )
                flags.update(result)
            except Exception:  # noqa: BLE001 - a model failure degrades to citation
                decision_source = "citation"
                model = None

        updated: dict[str, SpecialistBrief] = {}
        verified = 0
        unverified = 0
        for agent, brief in briefs.items():
            claims: list[Claim] = []
            for index, claim in enumerate(brief.claims):
                passed = flags.get((agent, index), False)
                if passed:
                    verified += 1
                else:
                    unverified += 1
                claims.append(replace(claim, verified=passed))
            updated[agent] = replace(brief, claims=tuple(claims))
        return EvidenceResult(
            briefs=updated,
            verified_claims=verified,
            unverified_claims=unverified,
            missing_evidence_ids=tuple(sorted(missing)),
            decision_source=decision_source,
            model=model,
        )

    def _model_pass(
        self,
        router: EvidenceRouterLike,
        candidates: Sequence[tuple[str, int, Claim]],
        known: Mapping[str, NewsItem],
        *,
        as_of: datetime,
    ) -> tuple[dict[tuple[str, int], bool], str, str]:
        claims_state: dict[str, object] = {}
        statements: list[str] = []
        for index, (_, _, claim) in enumerate(candidates, start=1):
            statements.append(claim.statement)
            evidence: list[dict[str, object]] = []
            for evidence_id in claim.evidence_ids:
                item = known[evidence_id]
                evidence.append(
                    {
                        "id": evidence_id,
                        "title": item.title,
                        "summary": item.summary,
                        "category": item.category.value,
                    }
                )
            claims_state[f"claim_{index}"] = {
                "statement": claim.statement,
                "evidence": evidence,
            }
        state: dict[str, object] = {
            "schema_version": 1,
            "as_of": as_of.isoformat(),
            "claims": claims_state,
        }
        questions_for(EVIDENCE, claims=statements)
        outcome = router.decide(
            "evidence_verifier",
            state=state,
            battery=EVIDENCE,
            task_id="analysis.evidence",
            claims=tuple(statements),
        )
        flags: dict[tuple[str, int], bool] = {}
        for index, (agent, claim_index, _) in enumerate(candidates, start=1):
            answer = outcome.judgments.answer(f"claim_{index}")
            probability = answer.noul if answer.noul is not None else 0.0
            flags[(agent, claim_index)] = probability >= self.min_probability
        return flags, outcome.source, outcome.model


__all__ = [
    "EvidenceError",
    "EvidenceResult",
    "EvidenceRouterLike",
    "EvidenceVerifier",
    "MAX_MODEL_CLAIMS",
    "MIN_NOUL_PROBABILITY",
]
