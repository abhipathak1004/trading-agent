"""A2 — Regime agent.

Answers: *should we trade at all today, and at what size?*

Ported from `pradeepsiddappa/indian-trading-agent` (Apache-2.0): the four-regime
classifier and the Daily Verdict decision table. Thresholds retuned for NSE and
extended with India VIX and market breadth.

Deterministic. No LLM. Same inputs must always produce the same verdict.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import polars as pl
import structlog

from pinak.core.calendar import resolve_trading_days
from pinak.core.config import RegimeConfig
from pinak.core.models import RegimeLabel, RegimeState, Verdict
from pinak.data.providers.nse_bhavcopy import NseBhavcopyProvider
from pinak.data.providers.nse_indices import NseIndicesProvider

log = structlog.get_logger(__name__)

TRADING_DAYS_PER_YEAR = 252


def classify_regime(
    close: float,
    sma_fast: float,
    sma_slow: float,
    vol_ratio: float,
    high_vol_ratio: float = 1.5,
) -> RegimeLabel:
    """Four-way label. Volatility overrides trend, as in the source model."""
    if vol_ratio > high_vol_ratio:
        return RegimeLabel.HIGH_VOL
    if close > sma_fast > sma_slow:
        return RegimeLabel.BULL
    if close < sma_fast < sma_slow:
        return RegimeLabel.BEAR
    return RegimeLabel.SIDEWAYS


def compute_verdict(caution: int, favorable: int) -> tuple[Verdict, float, int]:
    """Daily Verdict decision table.

    Caution is weighted above favour: missing a good day is cheaper than trading
    into an unexpected event. Returns ``(verdict, size_multiplier, max_positions)``.
    """
    if caution >= 2:
        return Verdict.RED, 0.0, 0
    if caution == 1:
        if favorable >= 1:
            return Verdict.YELLOW, 0.75, 3
        return Verdict.YELLOW, 0.5, 2
    if favorable >= 2:
        return Verdict.GREEN, 1.0, 5
    if favorable == 1:
        return Verdict.GREEN, 1.0, 4
    return Verdict.YELLOW, 0.75, 2


class RegimeAgent:
    """Deterministic market-permission gate for the whole desk."""

    def __init__(
        self,
        indices: NseIndicesProvider,
        bhavcopy: NseBhavcopyProvider,
        config: RegimeConfig,
    ) -> None:
        self.indices = indices
        self.bhavcopy = bhavcopy
        self.config = config

    def run(self, as_of: date | None = None) -> RegimeState:
        cfg = self.config
        end = as_of or date.today()

        days = resolve_trading_days(
            self.indices.fetch, end, cfg.lookback_days, minimum=cfg.lookback_days
        )
        all_indices = pl.concat(
            [self.indices.fetch(day) for day in days], how="vertical_relaxed"
        )
        nifty = all_indices.filter(pl.col("index") == cfg.index).sort("date")
        if nifty.height < cfg.sma_slow:
            raise ValueError(
                f"{cfg.index}: {nifty.height} closes, need {cfg.sma_slow} for SMA"
            )

        closes = nifty["close"]
        close = float(closes[-1])
        sma_fast = float(closes.tail(cfg.sma_fast).mean() or 0.0)
        sma_slow = float(closes.tail(cfg.sma_slow).mean() or 0.0)

        log_returns = (closes / closes.shift(1)).log()
        realized = float(log_returns.tail(cfg.vol_short).std() or 0.0) * math.sqrt(
            TRADING_DAYS_PER_YEAR
        ) * 100
        baseline = float(log_returns.tail(cfg.vol_long).std() or 0.0) * math.sqrt(
            TRADING_DAYS_PER_YEAR
        ) * 100
        vol_ratio = realized / baseline if baseline > 0 else 1.0

        vix = self._latest_vix(all_indices, cfg.vix_index)
        last_day = days[-1]
        breadth = self._breadth(last_day)

        label = classify_regime(close, sma_fast, sma_slow, vol_ratio, cfg.high_vol_ratio)

        caution = 0
        favorable = 0
        reasons: list[str] = []

        if label is RegimeLabel.BULL:
            favorable += 1
            reasons.append(
                f"trend: close {close:.0f} > SMA{cfg.sma_fast} {sma_fast:.0f} "
                f"> SMA{cfg.sma_slow} {sma_slow:.0f}"
            )
        elif label is RegimeLabel.BEAR:
            caution += 1
            reasons.append(
                f"trend: close {close:.0f} < SMA{cfg.sma_fast} {sma_fast:.0f} "
                f"< SMA{cfg.sma_slow} {sma_slow:.0f}"
            )
        elif label is RegimeLabel.HIGH_VOL:
            caution += 1
            reasons.append(
                f"vol: 20d {realized:.1f}% is {vol_ratio:.2f}x the 120d baseline"
            )
        else:
            reasons.append("trend: moving averages not aligned")

        if breadth < cfg.breadth_weak:
            caution += 1
            reasons.append(f"breadth: only {breadth:.0%} advancing")
        elif breadth > cfg.breadth_strong:
            favorable += 1
            reasons.append(f"breadth: {breadth:.0%} advancing")

        if vix is not None:
            if vix >= cfg.vix_red:
                caution += 2
                reasons.append(f"vix: {vix:.1f} >= {cfg.vix_red:.0f}")
            elif vix >= cfg.vix_caution:
                caution += 1
                reasons.append(f"vix: {vix:.1f} >= {cfg.vix_caution:.0f}")

        verdict, size_multiplier, max_positions = compute_verdict(caution, favorable)
        if label is RegimeLabel.SIDEWAYS:
            max_positions = min(max_positions, 3)

        state = RegimeState(
            as_of=last_day,
            label=label,
            verdict=verdict,
            size_multiplier=size_multiplier,
            max_new_positions=max_positions,
            nifty_close=round(close, 2),
            sma_fast=round(sma_fast, 2),
            sma_slow=round(sma_slow, 2),
            realized_vol=round(realized, 2),
            baseline_vol=round(baseline, 2),
            vol_ratio=round(vol_ratio, 3),
            vix=None if vix is None else round(vix, 2),
            breadth=round(breadth, 3),
            caution=caution,
            favorable=favorable,
            reasons=reasons,
        )
        log.info(
            "regime.done",
            as_of=str(last_day),
            label=label.value,
            verdict=verdict.value,
            size=size_multiplier,
            max_positions=max_positions,
        )
        return state

    @staticmethod
    def _latest_vix(all_indices: pl.DataFrame, vix_index: str) -> float | None:
        rows = all_indices.filter(pl.col("index") == vix_index).sort("date")
        if rows.height == 0:
            return None
        return float(rows["close"][-1])

    def _breadth(self, as_of: date) -> float:
        """Share of EQ symbols closing up versus the previous session."""
        days = resolve_trading_days(self.bhavcopy.fetch, as_of, 2, minimum=2)
        previous_day, last_day = days
        previous = self.bhavcopy.fetch(previous_day).select(
            "symbol", pl.col("close").alias("previous")
        )
        latest = self.bhavcopy.fetch(last_day).select("symbol", "close")
        joined = latest.join(previous, on="symbol", how="inner")

        advancing = int((joined["close"] > joined["previous"]).sum() or 0)
        declining = int((joined["close"] < joined["previous"]).sum() or 0)
        total = advancing + declining
        return advancing / total if total else 0.5


def write_report(state: RegimeState, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = state.as_of.isoformat()
    json_path = out_dir / f"regime-{stem}.json"
    markdown_path = out_dir / f"regime-{stem}.md"

    json_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    lines = [
        f"# Regime — {stem}",
        "",
        f"- **Label:** {state.label.value}",
        f"- **Verdict:** {state.verdict.value}",
        f"- **Size multiplier:** {state.size_multiplier:.2f}x",
        f"- **Max new positions:** {state.max_new_positions}",
        f"- **NIFTY 50:** {state.nifty_close:.2f}",
        f"- **SMA fast / slow:** {state.sma_fast:.2f} / {state.sma_slow:.2f}",
        f"- **Realised vol 20d:** {state.realized_vol:.2f}%",
        f"- **Baseline vol 120d:** {state.baseline_vol:.2f}% ({state.vol_ratio:.2f}x)",
        f"- **India VIX:** {'—' if state.vix is None else f'{state.vix:.2f}'}",
        f"- **Breadth:** {state.breadth:.1%} advancing",
        f"- **Flags:** {state.caution} caution / {state.favorable} favourable",
        "",
        "## Reasons",
        "",
    ]
    lines += [f"- {reason}" for reason in state.reasons] or ["- none"]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path
