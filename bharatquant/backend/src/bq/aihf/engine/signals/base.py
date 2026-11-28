"""Interfaces for models that produce investment views, independent of sizing."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import ClassVar, Literal, TypeAlias

from bq.aihf.engine.data.protocol import DataClient
from bq.aihf.engine.models import Signal
from bq.aihf.engine.universe.models import Factor

InvestmentApproach: TypeAlias = Literal["long_only", "long_short"]


class AlphaModel(ABC):
    """Abstract base for all alpha models. Forms a view, returns a Signal."""

    # Every concrete registered analyst declares its own approach. This is
    # permission metadata, independent of its signed opinion or LLM prompt.
    investment_approach: ClassVar[InvestmentApproach]
    # Trading days over which a view is expected to play out. The scorecard
    # grades the view there: a value investor's call at a year, an earnings
    # drift model's at the weeks before the next print.
    horizon_days: ClassVar[int]
    # What the analyst wants to see in a name before reading it. A fund with
    # a universe rule ranks the eligible names on these and sends only the
    # top of the rank here. Empty means no preference: the analyst reads what
    # the other seats in its strategy pick.
    screen: ClassVar[tuple[Factor, ...]]

    @property
    @abstractmethod
    def name(self) -> str:
        """Model identifier (e.g. 'pead', 'buffett')."""
        ...

    @abstractmethod
    def predict(
        self,
        ticker: str,
        date: str,
        data_client: DataClient,
    ) -> Signal:
        """Form a point-in-time view on *ticker* as of *date*.

        Use only information available on or before *date*. Return conviction
        in [-1, +1]; zero is neutral. Set metadata["abstained"] to True when
        the model cannot form a view.
        """
        ...


class QuantModel(AlphaModel):
    """Base for pure-math alpha models (no LLM).

    Houses shared numeric helpers. Subclass this for quant signals like
    PEAD or regime detection.
    """

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        """Convert to float, returning *default* for NaN / None / errors."""
        if value is None:
            return default
        try:
            f = float(value)
            return default if (math.isnan(f) or math.isinf(f)) else f
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _percentile_rank(value: float, values: list[float]) -> float:
        """Return the percentile rank (0-100) of *value* within *values*."""
        if not values:
            return 50.0
        below = sum(1 for v in values if v < value)
        return (below / len(values)) * 100.0

    @staticmethod
    def _normalize_to_signal(raw: float, low: float = -1.0, high: float = 1.0) -> float:
        """Clamp *raw* into [low, high]."""
        return max(low, min(high, raw))

    @staticmethod
    def _sigmoid(x: float, scale: float = 5.0) -> float:
        """Map an unbounded value into (-1, +1) via scaled tanh."""
        return math.tanh(x * scale)
