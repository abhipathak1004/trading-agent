"""v2 feature engineering.

Point-in-time fundamentals snapshots for LLM analysts; later: earnings
surprise features, KPI momentum, cross-sector lead-lag, feature importance
(MDA/MDI/SFI).
"""

from bq.aihf.engine.features.snapshot import (
    FundamentalsSnapshot,
    InsufficientData,
    PeriodFundamentals,
    build_snapshot,
)

__all__ = [
    "FundamentalsSnapshot",
    "InsufficientData",
    "PeriodFundamentals",
    "build_snapshot",
]
