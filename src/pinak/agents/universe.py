"""A1 — Universe agent.

Answers: *what can we trade today?*

Takes the last N NSE bhavcopy files, computes liquidity and volatility per
symbol, applies the hard eligibility gates, and ranks by 20-day traded value.
Nothing downstream may see a symbol that failed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl
import structlog

from pinak.core.calendar import resolve_trading_days
from pinak.core.config import UniverseConfig
from pinak.core.models import RejectReason
from pinak.data.providers.nse_bhavcopy import NseBhavcopyProvider

log = structlog.get_logger(__name__)

CRORE = 1e7


@dataclass(slots=True)
class UniverseResult:
    as_of: date
    frame: pl.DataFrame

    @property
    def tradable(self) -> pl.DataFrame:
        return self.frame.filter(pl.col("tradable"))

    @property
    def selected(self) -> pl.DataFrame:
        return self.frame.filter(pl.col("selected"))


class UniverseAgent:
    """Deterministic eligibility gate for the whole desk."""

    def __init__(self, provider: NseBhavcopyProvider, config: UniverseConfig) -> None:
        self.provider = provider
        self.config = config

    def run(self, as_of: date | None = None) -> UniverseResult:
        end = as_of or date.today()
        days = self._trading_days(end)
        frames = [self.provider.fetch(day) for day in days]
        daily = pl.concat(frames, how="vertical_relaxed").sort(["symbol", "date"])

        daily = daily.with_columns(
            pl.col("close")
            .shift(1)
            .over("symbol")
            .fill_null(pl.col("close"))
            .alias("prev_close")
        ).with_columns(
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - pl.col("prev_close")).abs(),
                (pl.col("low") - pl.col("prev_close")).abs(),
            ).alias("true_range")
        )

        agg = daily.group_by("symbol").agg(
            pl.col("close").sort_by("date").last().alias("close"),
            pl.col("turnover").sort_by("date").last().alias("last_turnover"),
            pl.col("turnover").mean().alias("avg_turnover"),
            pl.col("true_range").mean().alias("atr"),
            pl.len().alias("history_days"),
            pl.col("delivery_pct").sort_by("date").last().alias("delivery_pct"),
        )

        agg = agg.with_columns(
            (pl.col("last_turnover") / CRORE).alias("turnover_cr"),
            (pl.col("avg_turnover") / CRORE).alias("adv20_cr"),
            (pl.col("atr") / pl.col("close") * 100).alias("atr_pct"),
        )

        rules: list[tuple[str, pl.Expr]] = [
            (RejectReason.PRICE_TOO_LOW.value, pl.col("close") < self.config.min_price),
            (RejectReason.ILLIQUID_ADV.value, pl.col("adv20_cr") < self.config.min_adv_cr),
            (
                RejectReason.LOW_TURNOVER.value,
                pl.col("turnover_cr") < self.config.min_turnover_cr,
            ),
            (
                RejectReason.SHORT_HISTORY.value,
                pl.col("history_days") < self.config.min_history_days,
            ),
        ]
        excluded = [s.strip().upper() for s in self.config.exclude_symbols if s.strip()]
        if excluded:
            rules.append((RejectReason.EXCLUDED.value, pl.col("symbol").is_in(excluded)))

        reason_exprs = [pl.when(cond).then(pl.lit(name)) for name, cond in rules]
        out = agg.with_columns(
            pl.concat_list(reason_exprs).list.drop_nulls().alias("reject_reasons")
        )
        out = out.with_columns(
            (pl.col("reject_reasons").list.len() == 0).alias("tradable")
        )

        out = out.sort("adv20_cr", descending=True).with_columns(
            pl.when(pl.col("tradable"))
            .then(pl.lit(1))
            .otherwise(pl.lit(0))
            .cum_sum()
            .alias("rank")
        )
        out = out.with_columns(
            pl.when(pl.col("tradable"))
            .then(pl.col("rank"))
            .otherwise(None)
            .cast(pl.Int64)
            .alias("rank")
        )
        out = out.with_columns(
            (pl.col("rank").is_not_null() & (pl.col("rank") <= self.config.top_n)).alias("selected")
        )

        out = out.select(
            "symbol",
            pl.col("close").round(2),
            pl.col("turnover_cr").round(2),
            pl.col("adv20_cr").round(2),
            pl.col("atr_pct").round(3),
            pl.col("delivery_pct").round(2),
            "history_days",
            "tradable",
            "reject_reasons",
            "rank",
            "selected",
        )

        result = UniverseResult(as_of=days[-1], frame=out)
        log.info(
            "universe.done",
            as_of=str(result.as_of),
            symbols=out.height,
            tradable=result.tradable.height,
            selected=result.selected.height,
        )
        return result

    def _trading_days(self, end: date) -> list[date]:
        """Walk back from ``end`` until we have enough real trading days."""
        return resolve_trading_days(
            self.provider.fetch,
            end,
            self.config.lookback_days,
            minimum=self.config.min_history_days,
        )


def write_report(result: UniverseResult, out_dir: Path) -> tuple[Path, Path]:
    """Persist the universe as Parquet plus a human-readable Markdown report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = result.as_of.isoformat()
    parquet_path = out_dir / f"universe-{stem}.parquet"
    markdown_path = out_dir / f"universe-{stem}.md"

    result.frame.write_parquet(parquet_path)

    counts: dict[str, int] = {}
    for reasons in result.frame["reject_reasons"].to_list():
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1

    lines = [
        f"# Universe — {stem}",
        "",
        f"- Symbols seen: **{result.frame.height}**",
        f"- Tradable: **{result.tradable.height}**",
        f"- Selected: **{result.selected.height}**",
        "",
        "## Rejections",
        "",
        "_A symbol can fail more than one gate, so counts overlap._",
        "",
    ]
    if counts:
        lines += ["| Reason | Count |", "|---|---|"]
        lines += [f"| {reason} | {count} |" for reason, count in sorted(counts.items())]
    else:
        lines.append("_none_")

    lines += [
        "",
        "## Selected",
        "",
        "| # | Symbol | Close | ADV20 ₹Cr | Turnover ₹Cr | ATR% | Deliv% |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in result.selected.iter_rows(named=True):
        delivery = "—" if row["delivery_pct"] is None else f"{row['delivery_pct']:.1f}"
        lines.append(
            f"| {row['rank']} | {row['symbol']} | {row['close']:.2f} | {row['adv20_cr']:.1f} | "
            f"{row['turnover_cr']:.1f} | {row['atr_pct']:.2f} | {delivery} |"
        )

    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return parquet_path, markdown_path
