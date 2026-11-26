"""Deterministic market-regime specialist.

Reads NIFTY 50, NIFTY Bank and India VIX through the existing index vendor
domain, then classifies the regime in code. It never asks a model to compute a
number or label a regime; the model may later read the resulting facts.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from bq.analysis.briefs import SpecialistBrief
from bq.compute.indicators import adx, atr, bars_to_frame, sma
from bq.data.symbols import resolve as resolve_symbol
from bq.data.vendors import Bar, BarInterval, DataDomain, VendorRouter

LOOKBACK_DAYS = 180
MIN_BARS = 60
TREND_ADX = 20.0
HIGH_VOL_ATR = 0.04
HIGH_VOL_VIX = 25.0
VIX_MEAN_MULTIPLE = 1.5
REGIMES = ("trend_up", "trend_down", "range", "high_vol_stress")


class RegimeError(ValueError):
    pass


def classify_regime(
    *,
    close: float,
    sma20: float,
    sma50: float,
    return20: float,
    adx14: float,
    vix: float,
    vix_mean: float,
    atr_pct: float,
) -> str:
    """Classify a regime from values that were computed elsewhere."""
    if (
        vix >= HIGH_VOL_VIX
        or (vix_mean > 0 and vix >= VIX_MEAN_MULTIPLE * vix_mean)
        or atr_pct >= HIGH_VOL_ATR
    ):
        return "high_vol_stress"
    if close > sma20 > sma50 and return20 > 0 and adx14 >= TREND_ADX:
        return "trend_up"
    if close < sma20 < sma50 and return20 < 0 and adx14 >= TREND_ADX:
        return "trend_down"
    return "range"


class MarketRegimeAgent:
    def __init__(self, router: VendorRouter) -> None:
        self.router = router

    def brief(self, *, as_of: datetime) -> SpecialistBrief:
        nifty, nifty_source = self._series("NIFTY", as_of)
        bank, _ = self._series("NIFTY BANK", as_of)
        vix, _ = self._series("INDIA VIX", as_of)

        nifty_frame = bars_to_frame(nifty)
        bank_frame = bars_to_frame(bank)
        vix_frame = bars_to_frame(vix)
        nifty_close = nifty_frame["close"]
        bank_close = bank_frame["close"]
        vix_close = vix_frame["close"]

        sma20 = sma(nifty_close, 20)
        sma50 = sma(nifty_close, 50)
        adx14 = adx(nifty_frame, 14)
        atr14 = atr(nifty_frame, 14)
        if sma20 is None or sma50 is None or adx14 is None or atr14 is None:
            raise RegimeError("Index history is too short for the regime model.")
        close = float(nifty_close.iloc[-1])
        if close <= 0:
            raise RegimeError("Index close is not positive.")
        return20 = float(nifty_close.iloc[-1] / nifty_close.iloc[-21] - 1.0)
        bank_return20 = float(bank_close.iloc[-1] / bank_close.iloc[-21] - 1.0)
        vix_latest = float(vix_close.iloc[-1])
        vix_mean = float(vix_close.iloc[-20:].mean())
        atr_pct = atr14 / close
        regime = classify_regime(
            close=close,
            sma20=sma20,
            sma50=sma50,
            return20=return20,
            adx14=adx14,
            vix=vix_latest,
            vix_mean=vix_mean,
            atr_pct=atr_pct,
        )
        facts = (
            f"Regime: {regime}.",
            (
                f"NIFTY 20d return {return20:+.2%}; close {close:.2f} vs "
                f"SMA20 {sma20:.2f} / SMA50 {sma50:.2f}."
            ),
            f"Trend strength ADX {adx14:.1f}; ATR {atr_pct:.2%} of index.",
            f"India VIX {vix_latest:.1f} vs 20d mean {vix_mean:.1f}.",
            f"NIFTY Bank relative 20d {bank_return20 - return20:+.2%}.",
        )
        return SpecialistBrief(
            agent="market_regime",
            symbol="INDEX:NIFTY 50",
            as_of=as_of,
            facts=facts,
            source=f"indices:{nifty_source}",
        )

    def _series(self, name: str, as_of: datetime) -> tuple[tuple[Bar, ...], str]:
        symbol = resolve_symbol(name)
        result = self.router.bars(
            symbol,
            interval=BarInterval.DAY_1,
            start=as_of - timedelta(days=LOOKBACK_DAYS),
            end=as_of,
            domain=DataDomain.INDICES,
        )
        bars = tuple(bar for bar in result.data if bar.event_at <= as_of)
        if len(bars) < MIN_BARS:
            raise RegimeError(f"Not enough {name} history for the regime model.")
        return bars, result.source


__all__ = [
    "MarketRegimeAgent",
    "REGIMES",
    "RegimeError",
    "classify_regime",
]
