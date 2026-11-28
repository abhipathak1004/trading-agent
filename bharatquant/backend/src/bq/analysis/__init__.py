"""Multi-agent technical analysis.

Structure adapted from TauricResearch/TradingAgents v0.7.1 (Apache-2.0) as a
design reference only; all code here is original. See
``docs/migration-tradingagents.md`` and ADR-013.
"""

from bq.analysis.technical import (
    SubScore,
    TechnicalAnalyst,
    TechnicalReport,
)

__all__ = [
    "SubScore",
    "TechnicalAnalyst",
    "TechnicalReport",
]
