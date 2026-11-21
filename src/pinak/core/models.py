"""Core data contracts shared by every agent."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


class RejectReason(StrEnum):
    """Why a symbol is not tradable today."""

    PRICE_TOO_LOW = "price_too_low"
    ILLIQUID_ADV = "illiquid_adv"
    LOW_TURNOVER = "low_turnover"
    SHORT_HISTORY = "short_history"
    EXCLUDED = "excluded"


class UniverseRow(BaseModel):
    """One symbol after A1 processing."""

    symbol: str
    close: float
    turnover_cr: float
    adv20_cr: float
    atr_pct: float
    delivery_pct: float | None = None
    history_days: int
    tradable: bool
    reject_reasons: list[RejectReason] = Field(default_factory=list)
    rank: int | None = None
    selected: bool = False


class MarketContext(BaseModel):
    """Shared context passed to downstream agents."""

    trading_date: date
    universe: list[UniverseRow] = Field(default_factory=list)


class RegimeLabel(StrEnum):
    BULL = "BULL"
    BEAR = "BEAR"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOL = "HIGH_VOL"


class Verdict(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class RegimeState(BaseModel):
    """A2 output — the market's permission to trade today."""

    as_of: date
    label: RegimeLabel
    verdict: Verdict
    size_multiplier: float
    max_new_positions: int
    nifty_close: float
    sma_fast: float
    sma_slow: float
    realized_vol: float
    baseline_vol: float
    vol_ratio: float
    vix: float | None
    breadth: float
    caution: int
    favorable: int
    reasons: list[str] = Field(default_factory=list)


class QuantProfile(BaseModel):
    """A4 output — one symbol's statistical profile."""

    symbol: str
    close: float
    ret_5d: float
    ret_20d: float
    rs_5d: float
    rs_20d: float
    rs_rank: float
    beta: float | None = None
    vol_20: float
    vol_120: float
    vol_ratio: float
    atr_pct: float
    zscore_20: float
    gap_median: float
    gap_fill_rate: float | None = None
    dist_52w_high: float
    dist_52w_low: float
    history_days: int
