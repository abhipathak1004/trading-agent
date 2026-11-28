"""Registered alpha models and their static investment approaches."""

from __future__ import annotations

from typing import cast

from bq.aihf.engine.signals.base import AlphaModel, InvestmentApproach, QuantModel
from bq.aihf.engine.signals.buffett import BuffettAgent
from bq.aihf.engine.signals.druckenmiller import DruckenmillerAgent
from bq.aihf.engine.signals.graham import GrahamAgent
from bq.aihf.engine.signals.llm_agent import LLMAgent
from bq.aihf.engine.signals.lynch import LynchAgent
from bq.aihf.engine.signals.munger import MungerAgent
from bq.aihf.engine.signals.pead import PEADModel
from bq.aihf.engine.universe.models import Factor

ALPHA_MODEL_REGISTRY: dict[str, type[AlphaModel]] = {
    # Quant models
    "pead": PEADModel,
    # LLM investor agents
    "buffett": BuffettAgent,
    "munger": MungerAgent,
    "graham": GrahamAgent,
    "lynch": LynchAgent,
    "druckenmiller": DruckenmillerAgent,
}


def get_investment_approach(name: str) -> InvestmentApproach:
    """Read an analyst's declared approach without constructing a model or client.

    Raise ValueError for an unknown analyst or missing or invalid metadata.
    Profiles must be declared on the registered class, not inherited.
    """
    if name not in ALPHA_MODEL_REGISTRY:
        raise ValueError(f"unknown analyst {name!r}; available: {', '.join(sorted(ALPHA_MODEL_REGISTRY))}")
    approach = vars(ALPHA_MODEL_REGISTRY[name]).get("investment_approach")
    if approach not in ("long_only", "long_short"):
        raise ValueError(f"analyst {name!r} must declare investment_approach as long_only or long_short")
    return cast(InvestmentApproach, approach)


def get_horizon_days(name: str) -> int:
    """Read an analyst's declared horizon, in trading days, without constructing it.

    Raise ValueError for an unknown analyst or a horizon that is missing,
    inherited, or not a positive whole number of days.
    """
    if name not in ALPHA_MODEL_REGISTRY:
        raise ValueError(f"unknown analyst {name!r}; available: {', '.join(sorted(ALPHA_MODEL_REGISTRY))}")
    horizon = vars(ALPHA_MODEL_REGISTRY[name]).get("horizon_days")
    if type(horizon) is not int or horizon <= 0:
        raise ValueError(f"analyst {name!r} must declare horizon_days as a positive number of trading days")
    return horizon


def get_screen(name: str) -> tuple[Factor, ...]:
    """Read the factors an analyst screens on, without constructing it.

    Raise ValueError for an unknown analyst or a screen that is missing,
    inherited, or not a tuple of factors. An empty tuple is a declaration:
    the analyst has no preference.
    """
    if name not in ALPHA_MODEL_REGISTRY:
        raise ValueError(f"unknown analyst {name!r}; available: {', '.join(sorted(ALPHA_MODEL_REGISTRY))}")
    screen = vars(ALPHA_MODEL_REGISTRY[name]).get("screen")
    if type(screen) is not tuple or not all(isinstance(factor, Factor) for factor in screen):
        raise ValueError(f"analyst {name!r} must declare screen as a tuple of factors (empty for no preference)")
    return screen


__all__ = [
    "AlphaModel",
    "InvestmentApproach",
    "QuantModel",
    "LLMAgent",
    "BuffettAgent",
    "MungerAgent",
    "GrahamAgent",
    "LynchAgent",
    "DruckenmillerAgent",
    "PEADModel",
    "ALPHA_MODEL_REGISTRY",
    "get_investment_approach",
    "get_horizon_days",
    "get_screen",
]
