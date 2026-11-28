"""v2 backtesting — simulate a fund over history.

The CLI-era result store (`research.py`: `save_result`, `summarize`,
`receipts`, `last_score`, `last_universe`) is not vendored: BharatQuant's
`ResearchStore` owns saved results under `<BQ_HOME>/aihf/research/`, because
the saved-result list is a surface the app's own routes serve.
"""

from bq.aihf.engine.backtesting.fund import (
    backtest_fund,
    build_schedule,
    DailyValuation,
    FundBacktestMetrics,
    FundBacktestResult,
    performance_metrics,
    rebalance_grid,
    ReplaySchedule,
)

__all__ = [
    "DailyValuation",
    "FundBacktestMetrics",
    "FundBacktestResult",
    "ReplaySchedule",
    "backtest_fund",
    "build_schedule",
    "performance_metrics",
    "rebalance_grid",
]
