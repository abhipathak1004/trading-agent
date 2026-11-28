"""Dense deterministic state for decision calls.

Adapted from the reference architecture in ``buberlo/jev-trader`` (MIT): the
model receives a compact numerical snapshot, never prose and never a request to
calculate anything. Floats are rounded, unavailable values are dropped, and the
serialized state is bounded before it can be sent to a provider.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Mapping

from bq.analysis.decision import TradePlan
from bq.analysis.technical import TechnicalReport
from bq.core import canonical_digest
from bq.llm_tokens import approximate_tokens

MAX_STATE_BYTES = 16 * 1024
MAX_STATE_TOKENS = 2_000
STATE_SCHEMA_VERSION = 1


class DecisionStateError(ValueError):
    pass


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise DecisionStateError("Decision state values must be finite.")
    return round(number, 6)


@dataclass(frozen=True, slots=True)
class DecisionState:
    as_of: str
    symbol: str
    timeframe: str
    last_close: float
    bar_count: int
    composite: float | None
    trend: float | None
    momentum: float | None
    volatility: float | None
    structure: float | None
    rsi_14: float | None
    atr_14: float | None
    adx_14: float | None
    volume_relative: float | None
    bollinger_bandwidth: float | None
    macd_histogram: float | None
    support_distance_pct: float | None
    resistance_distance_pct: float | None
    data_freshness: str
    has_plan: bool = False
    proposed_action: str | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    target_price: float | None = None
    reward_risk: float | None = None
    notional_paise: int | None = None

    def __post_init__(self) -> None:
        if not self.as_of or not self.symbol or not self.timeframe:
            raise DecisionStateError(
                "A decision state needs as_of, symbol and timeframe."
            )
        if self.last_close <= 0 or not math.isfinite(self.last_close):
            raise DecisionStateError("A decision state needs a positive finite close.")
        if self.bar_count < 0:
            raise DecisionStateError("Bar count cannot be negative.")
        if self.data_freshness not in {"realtime_kite", "delayed_yfinance"}:
            raise DecisionStateError("Unknown data freshness label.")

    def to_state_dict(self) -> dict[str, object]:
        """The exact payload sent to a decision provider."""
        values: dict[str, float | None] = {
            "composite": self.composite,
            "trend": self.trend,
            "momentum": self.momentum,
            "volatility": self.volatility,
            "structure": self.structure,
            "rsi_14": self.rsi_14,
            "atr_14": self.atr_14,
            "adx_14": self.adx_14,
            "volume_relative": self.volume_relative,
            "bollinger_bandwidth": self.bollinger_bandwidth,
            "macd_histogram": self.macd_histogram,
            "support_distance_pct": self.support_distance_pct,
            "resistance_distance_pct": self.resistance_distance_pct,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "reward_risk": self.reward_risk,
        }
        payload: dict[str, object] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "as_of": self.as_of,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "last_close": _round(self.last_close),
            "bar_count": self.bar_count,
            "data_freshness": self.data_freshness,
            "has_plan": self.has_plan,
        }
        if self.proposed_action is not None:
            payload["proposed_action"] = self.proposed_action
        if self.notional_paise is not None:
            payload["notional_paise"] = self.notional_paise
        for key, value in values.items():
            rounded = _round(value)
            if rounded is not None:
                payload[key] = rounded
        self._check_size(payload)
        return payload

    def serialized(self) -> str:
        return json.dumps(
            self.to_state_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def digest(self) -> str:
        return canonical_digest(self.to_state_dict())

    @staticmethod
    def _check_size(payload: Mapping[str, object]) -> None:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            raise DecisionStateError("Decision state exceeds the byte limit.")
        if approximate_tokens(encoded) > MAX_STATE_TOKENS:
            raise DecisionStateError("Decision state exceeds the token budget.")


def _distance_pct(level: float | None, close: float) -> float | None:
    if level is None or close <= 0:
        return None
    return (level - close) / close


def build_decision_state(
    report: TechnicalReport,
    *,
    data_freshness: str,
    plan: TradePlan | None = None,
) -> DecisionState:
    """Build the compact state from an already-computed technical report and plan."""
    close = float(report.last_close)
    indicators = report.indicators
    swings = indicators.swings
    support = None if swings is None else swings.support
    resistance = None if swings is None else swings.resistance
    state = DecisionState(
        as_of=report.as_of.isoformat(),
        symbol=report.symbol,
        timeframe=report.timeframe,
        last_close=close,
        bar_count=report.bar_count,
        composite=report.composite,
        trend=None if report.trend is None else report.trend.value,
        momentum=None if report.momentum is None else report.momentum.value,
        volatility=None if report.volatility is None else report.volatility.value,
        structure=None if report.structure is None else report.structure.value,
        rsi_14=indicators.rsi_14,
        atr_14=indicators.atr_14,
        adx_14=indicators.adx_14,
        volume_relative=(
            None if indicators.volume is None else indicators.volume.relative
        ),
        bollinger_bandwidth=(
            None if indicators.bollinger is None else indicators.bollinger.bandwidth
        ),
        macd_histogram=(None if indicators.macd is None else indicators.macd.histogram),
        support_distance_pct=_distance_pct(support, close),
        resistance_distance_pct=_distance_pct(resistance, close),
        data_freshness=data_freshness,
        has_plan=plan is not None,
        proposed_action=None if plan is None else plan.signal.value,
        entry_price=None if plan is None else float(plan.entry_price),
        stop_loss=None if plan is None else float(plan.stop_loss),
        target_price=None if plan is None else float(plan.target_price),
        reward_risk=None if plan is None else float(plan.reward_risk),
        notional_paise=None if plan is None else plan.notional_paise().value,
    )
    state.to_state_dict()  # enforce the size/token budget at construction time
    return state


__all__ = [
    "DecisionState",
    "DecisionStateError",
    "MAX_STATE_BYTES",
    "MAX_STATE_TOKENS",
    "build_decision_state",
]
