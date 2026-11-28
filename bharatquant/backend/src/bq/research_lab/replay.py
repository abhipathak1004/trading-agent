"""Frozen replay and walk-forward scoring (ADR-054).

A replay is only meaningful if it runs on the inputs the run recorded. So the
first act is :func:`bq.research_lab.freeze.require_frozen_manifest`: a tampered
or mismatched slice aborts before a single bar is read, and no child run is
written.

Walk-forward uses the simulator's existing shape: indicators are computed over
*all* supplied bars, while trades are only taken inside ``[start, end]``. Passing
the bars before a fold's start therefore keeps the indicator warm-up without
scoring anything from the training window.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import polars as pl

from bq.backtest import BacktestMetrics, BacktestTrade, run_backtest
from bq.data.vendors import Bar
from bq.paper_charges import ChargeSchedule, IndiaCostModel
from bq.research_lab.freeze import require_frozen_manifest
from bq.strategy_dsl import StrategyDefinition

MIN_FOLDS: Final = 2
MAX_FOLDS: Final = 20


class ReplayError(ValueError):
    """A replay cannot be produced from the inputs as described."""


@dataclass(frozen=True, slots=True)
class WalkForwardSpec:
    """Split the evaluation window into ``folds`` contiguous out-of-sample spans.

    ``holdout_bars`` reserves that many trailing bars as a final window excluded
    from the folds, so parameters are never chosen on the data they are reported
    against.
    """

    folds: int = 3
    holdout_bars: int = 0


@dataclass(frozen=True, slots=True)
class ReplayResult:
    manifest_sha256: str
    metrics: BacktestMetrics
    trades: tuple[BacktestTrade, ...]
    folds: tuple[dict[str, object], ...]
    holdout: dict[str, object] | None
    bars: int

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_sha256": self.manifest_sha256,
            "metrics": self.metrics.to_dict(),
            "trades": [trade.to_dict() for trade in self.trades],
            "folds": list(self.folds),
            "holdout": self.holdout,
            "bars": self.bars,
        }


def bars_from_frame(frame: pl.DataFrame) -> dict[str, tuple[Bar, ...]]:
    """Turn a frozen frame back into the vendor ``Bar`` shape the simulator reads."""
    required = {"symbol", "ts", "open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ReplayError(f"Frozen inputs are missing columns: {sorted(missing)}.")
    grouped: dict[str, list[Bar]] = {}
    for row in frame.sort(["symbol", "ts"]).iter_rows(named=True):
        symbol = str(row["symbol"])
        grouped.setdefault(symbol, []).append(
            Bar(
                event_at=row["ts"],
                open=_rupees(row["open"]),
                high=_rupees(row["high"]),
                low=_rupees(row["low"]),
                close=_rupees(row["close"]),
                volume=int(row["volume"]),
            )
        )
    if not grouped:
        raise ReplayError("Frozen inputs contain no bars.")
    return {symbol: tuple(bars) for symbol, bars in grouped.items()}


def _rupees(paise: object) -> Decimal:
    return (Decimal(int(str(paise))) / Decimal(100)).quantize(Decimal("0.01"))


def _window_bars(
    bars: Mapping[str, tuple[Bar, ...]], *, end: datetime
) -> dict[str, tuple[Bar, ...]]:
    return {
        symbol: tuple(bar for bar in series if bar.event_at <= end)
        for symbol, series in bars.items()
    }


def _trading_dates(bars: Mapping[str, tuple[Bar, ...]]) -> list[datetime]:
    return sorted({bar.event_at for series in bars.values() for bar in series})


def _fold_report(
    definition: StrategyDefinition,
    bars: Mapping[str, tuple[Bar, ...]],
    *,
    start: datetime,
    end: datetime,
    initial_capital_paise: int,
    schedule: ChargeSchedule | None,
    label: int,
) -> dict[str, object]:
    result = run_backtest(
        definition,
        _window_bars(bars, end=end),
        start=start,
        end=end,
        initial_capital_paise=initial_capital_paise,
        charge_schedule=schedule,
    )
    return {
        "fold": label,
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "metrics": result.metrics.to_dict(),
        "trades": [trade.to_dict() for trade in result.trades],
    }


def replay_from_frozen(
    parquet_path: Path | str,
    expected_content_sha256: str,
    definition: StrategyDefinition,
    *,
    start: datetime,
    end: datetime,
    initial_capital_paise: int,
    cost_model: IndiaCostModel | None = None,
    walk_forward: WalkForwardSpec | None = None,
) -> ReplayResult:
    """Re-run ``definition`` on a verified frozen slice, with no network access."""
    require_frozen_manifest(parquet_path, expected_content_sha256)
    frame = pl.read_parquet(str(parquet_path))
    bars = bars_from_frame(frame)
    model = cost_model or IndiaCostModel()
    schedule = model.schedule

    full = run_backtest(
        definition,
        bars,
        start=start,
        end=end,
        initial_capital_paise=initial_capital_paise,
        charge_schedule=schedule,
    )

    folds: tuple[dict[str, object], ...] = ()
    holdout: dict[str, object] | None = None
    if walk_forward is not None:
        if not MIN_FOLDS <= walk_forward.folds <= MAX_FOLDS:
            raise ReplayError(f"Walk-forward needs {MIN_FOLDS} to {MAX_FOLDS} folds.")
        dates = [day for day in _trading_dates(bars) if start <= day <= end]
        reserved = walk_forward.holdout_bars
        if reserved < 0 or reserved >= len(dates):
            raise ReplayError("The holdout window leaves no folds to score.")
        evaluable = dates[: len(dates) - reserved] if reserved else dates
        if len(evaluable) < walk_forward.folds:
            raise ReplayError("Not enough dates for the requested folds.")
        size = len(evaluable) // walk_forward.folds
        reports: list[dict[str, object]] = []
        for index in range(walk_forward.folds):
            first = index * size
            last = (
                len(evaluable)
                if index == walk_forward.folds - 1
                else (index + 1) * size
            )
            reports.append(
                _fold_report(
                    definition,
                    bars,
                    start=evaluable[first],
                    end=evaluable[last - 1],
                    initial_capital_paise=initial_capital_paise,
                    schedule=schedule,
                    label=index + 1,
                )
            )
        folds = tuple(reports)
        if reserved:
            holdout = _fold_report(
                definition,
                bars,
                start=dates[len(dates) - reserved],
                end=dates[-1],
                initial_capital_paise=initial_capital_paise,
                schedule=schedule,
                label=0,
            )

    return ReplayResult(
        manifest_sha256=expected_content_sha256,
        metrics=full.metrics,
        trades=full.trades,
        folds=folds,
        holdout=holdout,
        bars=frame.height,
    )


def trades_csv(trades: Sequence[BacktestTrade]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "symbol",
            "entry_at",
            "exit_at",
            "quantity",
            "entry_price_paise",
            "exit_price_paise",
            "gross_pnl_paise",
            "charges_paise",
            "net_pnl_paise",
            "exit_reason",
        ]
    )
    for trade in trades:
        writer.writerow(
            [
                trade.symbol,
                trade.entry_at.isoformat(),
                trade.exit_at.isoformat(),
                trade.quantity,
                trade.entry_price_paise,
                trade.exit_price_paise,
                trade.gross_pnl_paise,
                trade.charges_paise,
                trade.net_pnl_paise,
                trade.exit_reason,
            ]
        )
    return buffer.getvalue()


def folds_csv(
    folds: Sequence[Mapping[str, object]], holdout: Mapping[str, object] | None
) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["fold", "start_utc", "end_utc", "trades", "net_pnl_paise", "return_bps"]
    )
    rows = list(folds)
    if holdout is not None:
        rows.append({**holdout, "fold": "holdout"})
    for row in rows:
        metrics = row.get("metrics")
        metrics_map = metrics if isinstance(metrics, Mapping) else {}
        writer.writerow(
            [
                row.get("fold"),
                row.get("start_utc"),
                row.get("end_utc"),
                metrics_map.get("trades"),
                metrics_map.get("net_pnl_paise"),
                metrics_map.get("return_bps"),
            ]
        )
    return buffer.getvalue()


__all__ = [
    "MAX_FOLDS",
    "MIN_FOLDS",
    "ReplayError",
    "ReplayResult",
    "WalkForwardSpec",
    "bars_from_frame",
    "folds_csv",
    "replay_from_frozen",
    "trades_csv",
]
