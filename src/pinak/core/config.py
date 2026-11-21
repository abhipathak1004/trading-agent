"""Configuration loading and validation."""

from __future__ import annotations

import tomllib
from datetime import time
from pathlib import Path

from pydantic import BaseModel, Field

# src/pinak/core/config.py -> repo root
ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "default.toml"


class UniverseConfig(BaseModel):
    """A1 Universe gates."""

    top_n: int = 150
    lookback_days: int = 20
    min_price: float = 50.0
    min_adv_cr: float = 10.0
    min_turnover_cr: float = 5.0
    min_history_days: int = 15
    exclude_symbols: list[str] = Field(default_factory=list)


class SessionConfig(BaseModel):
    """Intraday timing rules (see PHASE-1-DECISIONS §3.1)."""

    first_entry: time = time(9, 20)
    last_entry: time = time(14, 30)
    max_holding_minutes: int = 90
    square_off: time = time(15, 20)
    safety_buffer_minutes: int = 5


class RegimeConfig(BaseModel):
    """A2 Regime thresholds. Ported from indian-trading-agent, retuned for NSE."""

    lookback_days: int = 220
    sma_fast: int = 50
    sma_slow: int = 200
    vol_short: int = 20
    vol_long: int = 120
    high_vol_ratio: float = 1.5
    vix_caution: float = 25.0
    vix_red: float = 30.0
    breadth_weak: float = 0.35
    breadth_strong: float = 0.65
    index: str = "Nifty 50"
    vix_index: str = "India VIX"


class QuantConfig(BaseModel):
    """A4 Quant windows and thresholds."""

    lookback_days: int = 250
    rs_windows: tuple[int, int] = (5, 20)
    beta_window: int = 60
    vol_short: int = 20
    vol_long: int = 120
    zscore_window: int = 20
    gap_window: int = 120
    gap_threshold: float = 0.005
    min_history: int = 120
    index: str = "Nifty 50"


class PathsConfig(BaseModel):
    data: Path = Path("data")
    reports: Path = Path("reports")


class Config(BaseModel):
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    quant: QuantConfig = Field(default_factory=QuantConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML, falling back to defaults."""
    target = path or CONFIG_PATH
    if not target.exists():
        return Config()
    with target.open("rb") as handle:
        raw = tomllib.load(handle)
    return Config.model_validate(raw)


def data_dir() -> Path:
    path = ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def reports_dir() -> Path:
    path = ROOT / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path
