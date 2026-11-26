"""Bull vs bear debate, and the judge that resolves it.

Structure adapted from TradingAgents' researcher roles (R3 design reference, see
``prompts.SOURCE``). The code is original.

Three design commitments:

* **The debate cannot invent numbers.** It receives
  :meth:`TechnicalReport.facts` and nothing else numeric. The prompt says so and
  the response is bounded to short strings.
* **The judge cannot exceed the deterministic ceiling.** The confidence ceiling
  is computed in code from the deterministic sub-scores. A model may lower it; it
  may never raise it. That is what stops "the model felt bullish" from becoming a
  larger position.
* **A provider failure is a labelled degradation, not a crash.** Callers catch
  :class:`DebateUnavailable` and continue with ``RULES_ONLY`` (plan §3, graceful
  degradation). The debate is advisory by construction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from bq.agent_prompts import PromptSource, resolve_prompt
from bq.analysis.prompts import (
    BEAR_SYSTEM,
    BEAR_TEMPLATE,
    BULL_SYSTEM,
    BULL_TEMPLATE,
    JUDGE_SYSTEM,
    JUDGE_TEMPLATE,
)
from bq.analysis.technical import TechnicalReport

MAX_ARGUMENT_CHARS = 700
MAX_REASONING_CHARS = 600
MAX_POINTS = 4


class DebateUnavailable(RuntimeError):
    """The debate could not run. Advisory stages degrade rather than fail."""


class ModelCaller(Protocol):
    """The slice of ``LLMService`` the debate needs. Tests inject a fake."""

    def complete(
        self,
        *,
        prompt: str,
        system: str = ...,
        task_id: str = ...,
        task_version: str = ...,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class Argument:
    side: str
    argument: str
    points: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "side": self.side,
            "argument": self.argument,
            "points": list(self.points),
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    verdict: str
    reasoning: str
    confidence: float
    ceiling: float
    clamped: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "ceiling": self.ceiling,
            "clamped": self.clamped,
        }


def _extract_json(text: str) -> dict[str, object]:
    """Pull one JSON object out of a reply, or raise ``DebateUnavailable``.

    The model is asked for bare JSON. A reply that wraps it in a fenced block is
    tolerated because that is a formatting slip, not a different answer; anything
    else is treated as a failed turn rather than being creatively repaired.
    """
    if not isinstance(text, str) or not text.strip():
        raise DebateUnavailable("The model returned no text.")
    candidate = text.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        last_fence = candidate.rfind("```")
        if first_newline != -1 and last_fence > first_newline:
            candidate = candidate[first_newline + 1 : last_fence].strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise DebateUnavailable("The model did not return valid JSON.") from exc
    if not isinstance(payload, dict):
        raise DebateUnavailable("The model returned JSON that is not an object.")
    return payload


def _bounded(text: object, *, limit: int, label: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise DebateUnavailable(f"The model omitted '{label}'.")
    value = text.strip()
    return value[:limit]


def _points(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    points: list[str] = []
    for item in value[:MAX_POINTS]:
        if isinstance(item, str) and item.strip():
            points.append(item.strip()[:200])
    return tuple(points)


def confidence_ceiling(report: TechnicalReport) -> float:
    """A deterministic cap on model confidence, from the sub-scores alone.

    A strong, fully-available read permits 0.8. Each missing dimension and each
    weak dimension lowers it. This is the number a model may not exceed.
    """
    total_possible = 4
    coverage = report.available_dimensions / total_possible
    composite = report.composite
    if composite is None:
        return 0.0
    strength = (composite + 1.0) / 2.0  # map [-1, 1] to [0, 1]
    return round(min(0.8, 0.8 * coverage * strength + 0.1), 4)


class Debate:
    """Runs rounds of bull/bear adversarial review and a judge verdict."""

    def __init__(
        self, caller: ModelCaller, *, prompts: PromptSource | None = None
    ) -> None:
        self._caller = caller
        self._prompts = prompts

    def _call(self, *, prompt: str, system: str, task_id: str) -> str:
        try:
            outcome = self._caller.complete(
                prompt=prompt, system=system, task_id=task_id, task_version="v1"
            )
        except Exception as exc:  # provider failures are a degradation, not a bug
            raise DebateUnavailable(
                f"The debate model call failed: {type(exc).__name__}."
            ) from exc
        text = getattr(outcome, "text", None)
        if not isinstance(text, str):
            raise DebateUnavailable("The model result had no text.")
        return text

    def _side(
        self,
        report: TechnicalReport,
        *,
        agent_id: str,
        system: str,
        template: str,
        side: str,
    ) -> Argument:
        resolved_system, resolved_template = resolve_prompt(
            self._prompts, agent_id, system, template
        )
        prompt = resolved_template.format(
            symbol=report.symbol,
            timeframe=report.timeframe,
            as_of=report.as_of.isoformat(),
            facts="\n".join(report.facts()),
        )
        payload = _extract_json(
            self._call(
                prompt=prompt, system=resolved_system, task_id=f"analysis.{side}"
            )
        )
        return Argument(
            side=side,
            argument=_bounded(
                payload.get("argument"), limit=MAX_ARGUMENT_CHARS, label="argument"
            ),
            points=_points(payload.get("strongest_points")),
        )

    def bull(self, report: TechnicalReport) -> Argument:
        return self._side(
            report,
            agent_id="bull_debater",
            system=BULL_SYSTEM,
            template=BULL_TEMPLATE,
            side="bull",
        )

    def bear(self, report: TechnicalReport) -> Argument:
        return self._side(
            report,
            agent_id="bear_debater",
            system=BEAR_SYSTEM,
            template=BEAR_TEMPLATE,
            side="bear",
        )

    def judge(
        self,
        report: TechnicalReport,
        *,
        bull: Argument,
        bear: Argument,
        ceiling: float,
    ) -> Verdict:
        judge_system, judge_template = resolve_prompt(
            self._prompts, "research_judge", JUDGE_SYSTEM, JUDGE_TEMPLATE
        )
        prompt = judge_template.format(
            symbol=report.symbol,
            timeframe=report.timeframe,
            as_of=report.as_of.isoformat(),
            confidence_ceiling=ceiling,
            facts="\n".join(report.facts()),
            bull=json.dumps(bull.to_dict(), ensure_ascii=False),
            bear=json.dumps(bear.to_dict(), ensure_ascii=False),
        )
        payload = _extract_json(
            self._call(prompt=prompt, system=judge_system, task_id="analysis.judge")
        )
        verdict = _bounded(payload.get("verdict"), limit=8, label="verdict").upper()
        if verdict not in {"BUY", "HOLD"}:
            raise DebateUnavailable("The judge returned an unknown verdict.")
        reasoning = _bounded(
            payload.get("reasoning"), limit=MAX_REASONING_CHARS, label="reasoning"
        )
        raw_confidence = payload.get("confidence")
        if isinstance(raw_confidence, bool) or not isinstance(
            raw_confidence, (int, float)
        ):
            raise DebateUnavailable("The judge omitted a numeric confidence.")
        proposed = float(raw_confidence)
        if not 0.0 <= proposed <= 1.0:
            raise DebateUnavailable("The judge returned confidence outside [0, 1].")
        clamped = proposed > ceiling
        return Verdict(
            verdict=verdict,
            reasoning=reasoning,
            confidence=min(proposed, ceiling),
            ceiling=ceiling,
            clamped=clamped,
        )


__all__ = [
    "Argument",
    "Debate",
    "DebateUnavailable",
    "MAX_ARGUMENT_CHARS",
    "ModelCaller",
    "Verdict",
    "confidence_ceiling",
]
