import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class StrategyKind(StrEnum):
    RULE = "RULE"
    LLM_DISCRETIONARY = "LLM_DISCRETIONARY"
    HYBRID = "HYBRID"


class StrategyLifecycle(StrEnum):
    DRAFT = "DRAFT"
    BACKTESTED = "BACKTESTED"
    PAPER_SHADOW = "PAPER_SHADOW"
    PAPER_ACTIVE = "PAPER_ACTIVE"


class StrategyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    strategy_id: str
    version: str
    kind: StrategyKind
    lifecycle: StrategyLifecycle
    knowledge_cutoff: datetime | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.strategy_id, str)
            or IDENTIFIER.fullmatch(self.strategy_id) is None
        ):
            raise StrategyError("Invalid strategy identifier.")
        if (
            not isinstance(self.version, str)
            or IDENTIFIER.fullmatch(self.version) is None
        ):
            raise StrategyError("Invalid strategy version.")
        if not isinstance(self.kind, StrategyKind):
            raise StrategyError("Invalid strategy kind.")
        if not isinstance(self.lifecycle, StrategyLifecycle):
            raise StrategyError("Invalid strategy lifecycle.")
        if self.kind is not StrategyKind.RULE:
            if self.knowledge_cutoff is None:
                raise StrategyError(
                    "LLM-dependent strategies require a knowledge cutoff."
                )
            if self.knowledge_cutoff.utcoffset() is None:
                raise StrategyError("Knowledge cutoff must be timezone-aware.")


def promote_strategy(
    strategy: StrategyVersion,
    target: StrategyLifecycle,
) -> StrategyVersion:
    if not isinstance(strategy, StrategyVersion):
        raise StrategyError("Strategy version is required.")
    if not isinstance(target, StrategyLifecycle):
        raise StrategyError("Invalid promotion target.")
    order = (
        StrategyLifecycle.DRAFT,
        StrategyLifecycle.BACKTESTED,
        StrategyLifecycle.PAPER_SHADOW,
        StrategyLifecycle.PAPER_ACTIVE,
    )
    if order.index(target) != order.index(strategy.lifecycle) + 1:
        raise StrategyError("Strategy lifecycle promotions must advance one stage.")
    return StrategyVersion(
        strategy_id=strategy.strategy_id,
        version=strategy.version,
        kind=strategy.kind,
        lifecycle=target,
        knowledge_cutoff=strategy.knowledge_cutoff,
    )


def replay_allowed(
    strategy: StrategyVersion,
    *,
    replay_start: datetime,
    replay_end: datetime,
) -> tuple[bool, str]:
    if not isinstance(strategy, StrategyVersion):
        raise StrategyError("Strategy version is required.")
    if replay_start.utcoffset() is None or replay_end.utcoffset() is None:
        raise StrategyError("Replay boundaries must be timezone-aware.")
    replay_start = replay_start.astimezone(UTC)
    replay_end = replay_end.astimezone(UTC)
    if replay_end < replay_start:
        raise StrategyError("Replay end cannot precede replay start.")
    if strategy.kind is StrategyKind.RULE:
        return True, "FORWARD_VALID"
    cutoff = strategy.knowledge_cutoff
    if cutoff is None:
        raise StrategyError("LLM-dependent strategy is missing its knowledge cutoff.")
    cutoff = cutoff.astimezone(UTC)
    if replay_start < cutoff:
        return False, "CONTAMINATED"
    return True, "FORWARD_ONLY"
