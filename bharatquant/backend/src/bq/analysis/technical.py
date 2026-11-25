"""The deterministic technical analyst.

Everything here is arithmetic over :class:`bq.compute.indicators.IconSnapshot`.
The model is not consulted: it never computes a number, it only later
*interprets* the :class:`TechnicalReport` this module produces (plan §3,
principle 11).

Why sub-scores rather than one blended number: a single "score" hides *why* a
setup looks good, and a debate cannot argue about it. Four independent
sub-scores — trend, momentum, volatility, structure — each with its own evidence
list, give the bull and bear advocates something concrete to disagree with.

Every sub-score is bounded to ``[-1, 1]`` with ``0`` neutral, so callers can
average or threshold them without knowing the internals. A sub-score whose inputs
are unavailable is ``None``, never ``0``: "neutral" and "unknown" must stay
distinguishable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bq.compute.indicators import IndicatorSnapshot, compute_snapshot
from bq.data.vendors.base import Bar

# Thresholds are here, named, so they are reviewable and testable rather than
# buried in an expression.
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
ADX_TRENDING = 25.0
ADX_RANGEBOUND = 20.0
VOLUME_SURGE = 1.5
DEEP_DRAWDOWN = 0.20

# A dimension needs at least this many bars to mean anything. Three bars can
# produce a max-drawdown and a VWAP, but neither is a volatility or structural
# read, so we report those dimensions unavailable rather than misleadingly
# precise.
MIN_BARS_TREND = 21
MIN_BARS_MOMENTUM = 27
MIN_BARS_VOLATILITY = 15
MIN_BARS_STRUCTURE = 21


@dataclass(frozen=True, slots=True)
class SubScore:
    """One dimension of the technical picture.

    ``value`` is bounded to ``[-1, 1]``. ``evidence`` is human-readable and is
    copied verbatim into the analyst prompt, so it must be true.
    """

    value: float
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not -1.0 <= self.value <= 1.0:
            raise ValueError("A sub-score must be within [-1, 1].")
        if not self.evidence:
            raise ValueError("A sub-score must explain itself.")


@dataclass(frozen=True, slots=True)
class TechnicalReport:
    """The single numeric input every later agent stage receives."""

    symbol: str
    as_of: datetime
    timeframe: str
    last_close: Decimal
    bar_count: int
    trend: SubScore | None
    momentum: SubScore | None
    volatility: SubScore | None
    structure: SubScore | None
    indicators: IndicatorSnapshot

    @property
    def scored_dimensions(self) -> tuple[SubScore, ...]:
        return tuple(
            score
            for score in (self.trend, self.momentum, self.volatility, self.structure)
            if score is not None
        )

    @property
    def composite(self) -> float | None:
        """Mean of the available sub-scores, or ``None`` if none are available.

        Deliberately *not* weighted: any weighting would be an unvalidated
        assumption, and the debate stage is where qualitative weighting belongs.
        """
        scores = self.scored_dimensions
        if not scores:
            return None
        return sum(score.value for score in scores) / len(scores)

    @property
    def available_dimensions(self) -> int:
        return len(self.scored_dimensions)

    def facts(self) -> tuple[str, ...]:
        """All evidence lines, in a stable order, for the prompt and the UI."""
        lines: list[str] = []
        for label, score in (
            ("Trend", self.trend),
            ("Momentum", self.momentum),
            ("Volatility", self.volatility),
            ("Structure", self.structure),
        ):
            if score is None:
                lines.append(f"{label}: unavailable (insufficient history)")
                continue
            lines.append(f"{label} ({score.value:+.2f}): " + "; ".join(score.evidence))
        return tuple(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "timeframe": self.timeframe,
            "last_close": str(self.last_close),
            "bar_count": self.bar_count,
            "composite": self.composite,
            "available_dimensions": self.available_dimensions,
            "sub_scores": {
                "trend": None if self.trend is None else self.trend.value,
                "momentum": None if self.momentum is None else self.momentum.value,
                "volatility": (
                    None if self.volatility is None else self.volatility.value
                ),
                "structure": None if self.structure is None else self.structure.value,
            },
            "facts": list(self.facts()),
            "indicators": self.indicators.to_dict(),
        }


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def _trend_score(snapshot: IndicatorSnapshot) -> SubScore | None:
    if snapshot.bar_count < MIN_BARS_TREND:
        return None
    evidence: list[str] = []
    signals: list[float] = []
    close = snapshot.close

    if snapshot.sma_20 is not None:
        relation = 1.0 if close > snapshot.sma_20 else -1.0
        signals.append(relation)
        evidence.append(
            f"close {close:.2f} {'above' if relation > 0 else 'below'} SMA20 "
            f"{snapshot.sma_20:.2f}"
        )
    if snapshot.sma_20 is not None and snapshot.sma_50 is not None:
        stack = 1.0 if snapshot.sma_20 > snapshot.sma_50 else -1.0
        signals.append(stack)
        evidence.append(
            f"SMA20 {'above' if stack > 0 else 'below'} SMA50 "
            f"({snapshot.sma_20:.2f} vs {snapshot.sma_50:.2f})"
        )
    if snapshot.adx_14 is not None:
        if snapshot.adx_14 >= ADX_TRENDING:
            evidence.append(f"ADX {snapshot.adx_14:.1f} confirms a trend")
        elif snapshot.adx_14 <= ADX_RANGEBOUND:
            evidence.append(f"ADX {snapshot.adx_14:.1f} suggests a range, not a trend")
            signals.append(0.0)
        else:
            evidence.append(f"ADX {snapshot.adx_14:.1f} is inconclusive")

    if not signals:
        return None
    return SubScore(value=_clamp(sum(signals) / len(signals)), evidence=tuple(evidence))


def _momentum_score(snapshot: IndicatorSnapshot) -> SubScore | None:
    if snapshot.bar_count < MIN_BARS_MOMENTUM:
        return None
    evidence: list[str] = []
    signals: list[float] = []

    if snapshot.rsi_14 is not None:
        value = snapshot.rsi_14
        if value >= RSI_OVERBOUGHT:
            signals.append(-0.5)
            evidence.append(f"RSI {value:.1f} is overbought")
        elif value <= RSI_OVERSOLD:
            signals.append(0.5)
            evidence.append(f"RSI {value:.1f} is oversold")
        else:
            signals.append((value - 50.0) / 50.0)
            evidence.append(f"RSI {value:.1f} is mid-range")
    if snapshot.macd is not None:
        histogram = snapshot.macd.histogram
        signals.append(1.0 if histogram > 0 else -1.0)
        evidence.append(
            f"MACD histogram {histogram:+.3f} "
            f"({'bullish' if histogram > 0 else 'bearish'} cross)"
        )

    if not signals:
        return None
    return SubScore(value=_clamp(sum(signals) / len(signals)), evidence=tuple(evidence))


def _volatility_score(snapshot: IndicatorSnapshot) -> SubScore | None:
    """High volatility is reported as *risk*, so the score is negative-heavy."""
    if snapshot.bar_count < MIN_BARS_VOLATILITY:
        return None
    evidence: list[str] = []
    signals: list[float] = []

    if snapshot.atr_14 is not None and snapshot.close:
        atr_pct = snapshot.atr_14 / snapshot.close
        # 3% daily ATR is treated as the neutral point for an Indian large cap.
        signal = _clamp((0.03 - atr_pct) / 0.03)
        signals.append(signal)
        evidence.append(f"ATR is {atr_pct * 100:.2f}% of price")
    if snapshot.bollinger is not None:
        width = snapshot.bollinger.bandwidth
        signals.append(_clamp((0.10 - width) / 0.10))
        evidence.append(f"Bollinger bandwidth {width * 100:.2f}%")
    if snapshot.max_drawdown is not None:
        drawdown = snapshot.max_drawdown
        if drawdown >= DEEP_DRAWDOWN:
            signals.append(-1.0)
            evidence.append(f"max drawdown {drawdown * 100:.1f}% over the window")
        else:
            signals.append(_clamp(-drawdown / DEEP_DRAWDOWN))
            evidence.append(f"max drawdown {drawdown * 100:.1f}% over the window")

    if not signals:
        return None
    return SubScore(value=_clamp(sum(signals) / len(signals)), evidence=tuple(evidence))


def _structure_score(snapshot: IndicatorSnapshot) -> SubScore | None:
    if snapshot.bar_count < MIN_BARS_STRUCTURE:
        return None
    evidence: list[str] = []
    signals: list[float] = []

    swings = snapshot.swings
    if (
        swings is not None
        and swings.support is not None
        and swings.resistance is not None
    ):
        span = swings.resistance - swings.support
        if span > 0:
            position = (snapshot.close - swings.support) / span
            # Near support is constructive for a long; near resistance is not.
            signals.append(_clamp((0.5 - position) * 2.0))
            evidence.append(
                f"price at {position * 100:.0f}% of the support-resistance range "
                f"({swings.support:.2f}\u2013{swings.resistance:.2f})"
            )
    elif (
        swings is not None and swings.resistance is None and swings.support is not None
    ):
        # No overhead resistance: price is at the top of its own range, which is
        # a breakout read rather than a "near resistance" one.
        signals.append(0.3)
        evidence.append(
            f"no confirmed resistance above price; nearest support {swings.support:.2f}"
        )
    elif (
        swings is not None and swings.support is None and swings.resistance is not None
    ):
        # Overhead resistance with no support in the window: price is at a low.
        signals.append(-0.3)
        evidence.append(
            f"no confirmed support below price; nearest resistance "
            f"{swings.resistance:.2f}"
        )
    if snapshot.vwap is not None:
        relation = 1.0 if snapshot.close > snapshot.vwap else -1.0
        signals.append(relation)
        evidence.append(
            f"close {'above' if relation > 0 else 'below'} VWAP {snapshot.vwap:.2f}"
        )
    if snapshot.volume is not None:
        relative = snapshot.volume.relative
        if relative >= VOLUME_SURGE:
            signals.append(signals[-1] if signals else 0.5)
            evidence.append(f"volume {relative:.2f}x its 20-bar average")
        else:
            evidence.append(f"volume {relative:.2f}x its 20-bar average")

    if not signals:
        return None
    return SubScore(value=_clamp(sum(signals) / len(signals)), evidence=tuple(evidence))


class TechnicalAnalyst:
    """Turns bars into a :class:`TechnicalReport`. Deterministic, no model."""

    def analyse(
        self,
        *,
        symbol: str,
        bars: Sequence[Bar],
        timeframe: str,
        as_of: datetime | None = None,
    ) -> TechnicalReport:
        """Compute a report. Bars dated after ``as_of`` are dropped first.

        Dropping here (not only upstream) means a caller that forgets to filter
        cannot leak a future bar into the report.
        """
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("A symbol is required.")
        if not bars:
            raise ValueError("At least one bar is required.")

        visible = (
            tuple(bar for bar in bars if bar.event_at <= as_of)
            if as_of is not None
            else tuple(bars)
        )
        if not visible:
            raise ValueError("No bars available at or before as_of.")

        snapshot = compute_snapshot(visible)
        last = visible[-1]
        return TechnicalReport(
            symbol=symbol,
            as_of=last.event_at,
            timeframe=timeframe,
            last_close=last.close,
            bar_count=len(visible),
            trend=_trend_score(snapshot),
            momentum=_momentum_score(snapshot),
            volatility=_volatility_score(snapshot),
            structure=_structure_score(snapshot),
            indicators=snapshot,
        )


__all__ = [
    "ADX_RANGEBOUND",
    "ADX_TRENDING",
    "DEEP_DRAWDOWN",
    "MIN_BARS_MOMENTUM",
    "MIN_BARS_STRUCTURE",
    "MIN_BARS_TREND",
    "MIN_BARS_VOLATILITY",
    "RSI_OVERBOUGHT",
    "RSI_OVERSOLD",
    "SubScore",
    "TechnicalAnalyst",
    "TechnicalReport",
    "VOLUME_SURGE",
]
