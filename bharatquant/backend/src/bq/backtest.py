"""Fast and event-driven daily backtests over the strict strategy DSL.

Both engines share the same trade rules and accounting. The fast engine
precomputes indicators; the event engine recomputes them bar by bar. Tests require
identical output on the same fixture, which is the parity gate for later engines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Mapping

from bq.core import paise_from_price
from bq.data.vendors import Bar
from bq.paper_charges import ChargeSchedule, compute_charges
from bq.strategy_dsl import EntryRule, ExitRule, StrategyDefinition


class BacktestError(ValueError):
    pass


class BacktestEngineName(StrEnum):
    FAST = "FAST"
    EVENT = "EVENT"


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
    entry_at: datetime
    exit_at: datetime
    entry_price_paise: int
    exit_price_paise: int
    quantity: int
    gross_pnl_paise: int
    charges_paise: int
    net_pnl_paise: int
    exit_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "entry_at": self.entry_at.isoformat(),
            "exit_at": self.exit_at.isoformat(),
            "entry_price_paise": str(self.entry_price_paise),
            "exit_price_paise": str(self.exit_price_paise),
            "quantity": self.quantity,
            "gross_pnl_paise": str(self.gross_pnl_paise),
            "charges_paise": str(self.charges_paise),
            "net_pnl_paise": str(self.net_pnl_paise),
            "exit_reason": self.exit_reason,
        }


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    trades: int
    wins: int
    losses: int
    gross_pnl_paise: int
    charges_paise: int
    net_pnl_paise: int
    final_capital_paise: int
    return_bps: int
    win_rate: float | None
    max_drawdown_paise: int

    def to_dict(self) -> dict[str, object]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "gross_pnl_paise": str(self.gross_pnl_paise),
            "charges_paise": str(self.charges_paise),
            "net_pnl_paise": str(self.net_pnl_paise),
            "final_capital_paise": str(self.final_capital_paise),
            "return_bps": self.return_bps,
            "win_rate": self.win_rate,
            "max_drawdown_paise": str(self.max_drawdown_paise),
        }


@dataclass(frozen=True, slots=True)
class BacktestResult:
    engine: BacktestEngineName
    strategy_id: str
    version: str
    start: datetime
    end: datetime
    initial_capital_paise: int
    metrics: BacktestMetrics
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine.value,
            "strategy_id": self.strategy_id,
            "version": self.version,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "initial_capital_paise": str(self.initial_capital_paise),
            "metrics": self.metrics.to_dict(),
            "trades": [trade.to_dict() for trade in self.trades],
            "equity_curve": [str(item) for item in self.equity_curve],
        }


class _Indicators:
    def __init__(
        self,
        bars: tuple[Bar, ...],
        *,
        lookback: int,
        atr_period: int,
        precompute: bool,
    ) -> None:
        self.bars = bars
        self.lookback = lookback
        self.atr_period = atr_period
        self.precompute = precompute
        self._sma: list[Decimal | None] = []
        self._atr: list[Decimal | None] = []
        self._high: list[Decimal | None] = []
        self._low: list[Decimal | None] = []
        if precompute:
            for index in range(len(bars)):
                self._sma.append(self._sma_value(index))
                self._atr.append(self._atr_value(index))
                self._high.append(self._high_value(index))
                self._low.append(self._low_value(index))

    def _sma_value(self, index: int) -> Decimal | None:
        if index + 1 < self.lookback:
            return None
        window = self.bars[index + 1 - self.lookback : index + 1]
        return sum((bar.close for bar in window), Decimal("0")) / Decimal(self.lookback)

    def _atr_value(self, index: int) -> Decimal | None:
        if index + 1 < self.atr_period:
            return None
        total = Decimal("0")
        for cursor in range(index + 1 - self.atr_period, index + 1):
            bar = self.bars[cursor]
            previous = self.bars[cursor - 1].close if cursor > 0 else bar.close
            total += max(
                bar.high - bar.low,
                abs(bar.high - previous),
                abs(bar.low - previous),
            )
        return total / Decimal(self.atr_period)

    def _high_value(self, index: int) -> Decimal | None:
        if index < self.lookback:
            return None
        return max(bar.high for bar in self.bars[index - self.lookback : index])

    def _low_value(self, index: int) -> Decimal | None:
        if index < self.lookback:
            return None
        return min(bar.low for bar in self.bars[index - self.lookback : index])

    def sma(self, index: int) -> Decimal | None:
        return self._sma[index] if self.precompute else self._sma_value(index)

    def atr(self, index: int) -> Decimal | None:
        return self._atr[index] if self.precompute else self._atr_value(index)

    def high(self, index: int) -> Decimal | None:
        return self._high[index] if self.precompute else self._high_value(index)

    def low(self, index: int) -> Decimal | None:
        return self._low[index] if self.precompute else self._low_value(index)


def _paise(value: Decimal) -> int:
    return paise_from_price(value).value


def _slip(price_paise: int, slippage_bps: int, *, buy: bool) -> int:
    if buy:
        return price_paise * (10_000 + slippage_bps) // 10_000
    return price_paise * (10_000 - slippage_bps) // 10_000


def _entry_signal(
    definition: StrategyDefinition,
    indicators: _Indicators,
    index: int,
) -> bool:
    if index < 1:
        return False
    bar = indicators.bars[index]
    if definition.entry_rule is EntryRule.SMA_CROSS:
        current = indicators.sma(index)
        previous = indicators.sma(index - 1)
        return (
            current is not None
            and previous is not None
            and bar.close > current
            and indicators.bars[index - 1].close <= previous
        )
    prior_high = indicators.high(index)
    return prior_high is not None and bar.close > prior_high


def _exit_signal(
    definition: StrategyDefinition,
    indicators: _Indicators,
    index: int,
) -> bool:
    bar = indicators.bars[index]
    if definition.exit_rule is ExitRule.SMA_CROSS_DOWN:
        current = indicators.sma(index)
        return current is not None and bar.close < current
    prior_low = indicators.low(index)
    return prior_low is not None and bar.close < prior_low


def _simulate(
    definition: StrategyDefinition,
    bars: tuple[Bar, ...],
    *,
    start: datetime,
    end: datetime,
    allocation_paise: int,
    engine: BacktestEngineName,
    charge_schedule: ChargeSchedule | None,
) -> tuple[BacktestTrade, ...]:
    if not bars:
        return ()
    ordered = tuple(sorted(bars, key=lambda bar: bar.event_at))
    indicators = _Indicators(
        ordered,
        lookback=definition.lookback,
        atr_period=definition.atr_period,
        precompute=engine is BacktestEngineName.FAST,
    )
    warmup = max(definition.lookback, definition.atr_period) + 1
    trades: list[BacktestTrade] = []
    position: dict[str, object] | None = None
    pending: tuple[int, Decimal] | None = None
    for index in range(warmup, len(ordered)):
        bar = ordered[index]
        if bar.event_at < start:
            if pending is not None and pending[0] <= index:
                pending = None
            continue
        if bar.event_at > end:
            break
        if position is not None:
            entry = int(str(position["entry"]))
            stop = int(str(position["stop"]))
            target = int(str(position["target"]))
            quantity = int(str(position["quantity"]))
            exit_price: int | None = None
            reason = ""
            if _paise(bar.low) <= stop:
                exit_price = _slip(stop, definition.slippage_bps, buy=False)
                reason = "STOP"
            elif _paise(bar.high) >= target:
                exit_price = _slip(target, definition.slippage_bps, buy=False)
                reason = "TARGET"
            elif _exit_signal(definition, indicators, index):
                exit_price = _slip(
                    _paise(bar.close), definition.slippage_bps, buy=False
                )
                reason = "RULE"
            if exit_price is not None:
                entry_charge = int(str(position["entry_charge"]))
                sell_charge = compute_charges(
                    side="SELL",
                    product="MIS",
                    quantity=quantity,
                    price_paise=exit_price,
                    schedule=charge_schedule,
                ).total_paise
                gross = (exit_price - entry) * quantity
                charges = entry_charge + sell_charge
                trades.append(
                    BacktestTrade(
                        symbol=definition.universe[0],
                        entry_at=position["entry_at"],  # type: ignore[arg-type]
                        exit_at=bar.event_at,
                        entry_price_paise=entry,
                        exit_price_paise=exit_price,
                        quantity=quantity,
                        gross_pnl_paise=gross,
                        charges_paise=charges,
                        net_pnl_paise=gross - charges,
                        exit_reason=reason,
                    )
                )
                position = None
                continue
        if position is None and pending is not None and pending[0] <= index:
            signal_index, pending_atr = pending
            entry_price = _slip(_paise(bar.open), definition.slippage_bps, buy=True)
            stop_distance = int(
                pending_atr * Decimal(str(definition.stop_atr_multiple))
            )
            target_distance = int(
                pending_atr * Decimal(str(definition.target_atr_multiple))
            )
            stop = entry_price - stop_distance
            target = entry_price + target_distance
            if stop > 0 and target > entry_price:
                risk_amount = allocation_paise * definition.risk_per_trade_bps // 10_000
                quantity = min(
                    risk_amount // max(1, entry_price - stop),
                    allocation_paise // entry_price,
                )
                if quantity > 0:
                    entry_charge = compute_charges(
                        side="BUY",
                        product="MIS",
                        quantity=quantity,
                        price_paise=entry_price,
                        schedule=charge_schedule,
                    ).total_paise
                    position = {
                        "entry": entry_price,
                        "stop": stop,
                        "target": target,
                        "quantity": quantity,
                        "entry_charge": entry_charge,
                        "entry_at": bar.event_at,
                        "signal_index": signal_index,
                    }
            pending = None
            if position is not None:
                # A stop or target can be touched on the entry bar.
                continue
        if position is None and pending is None:
            if _entry_signal(definition, indicators, index):
                atr_value = indicators.atr(index)
                if atr_value is not None and atr_value > 0 and index + 1 < len(ordered):
                    pending = (index, atr_value)
    if position is not None:
        last = ordered[-1]
        if start <= last.event_at <= end:
            exit_price = _slip(_paise(last.close), definition.slippage_bps, buy=False)
            entry = int(str(position["entry"]))
            quantity = int(str(position["quantity"]))
            entry_charge = int(str(position["entry_charge"]))
            sell_charge = compute_charges(
                side="SELL",
                product="MIS",
                quantity=quantity,
                price_paise=exit_price,
                schedule=charge_schedule,
            ).total_paise
            gross = (exit_price - entry) * quantity
            charges = entry_charge + sell_charge
            trades.append(
                BacktestTrade(
                    symbol=definition.universe[0],
                    entry_at=position["entry_at"],  # type: ignore[arg-type]
                    exit_at=last.event_at,
                    entry_price_paise=entry,
                    exit_price_paise=exit_price,
                    quantity=quantity,
                    gross_pnl_paise=gross,
                    charges_paise=charges,
                    net_pnl_paise=gross - charges,
                    exit_reason="END",
                )
            )
    return tuple(trades)


def _metrics(
    trades: tuple[BacktestTrade, ...], *, initial_capital_paise: int
) -> tuple[BacktestMetrics, tuple[int, ...]]:
    gross = sum(item.gross_pnl_paise for item in trades)
    charges = sum(item.charges_paise for item in trades)
    net = sum(item.net_pnl_paise for item in trades)
    wins = sum(1 for item in trades if item.net_pnl_paise > 0)
    losses = sum(1 for item in trades if item.net_pnl_paise < 0)
    equity = initial_capital_paise
    curve: list[int] = []
    peak = equity
    max_drawdown = 0
    for item in trades:
        equity += item.net_pnl_paise
        curve.append(equity)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return (
        BacktestMetrics(
            trades=len(trades),
            wins=wins,
            losses=losses,
            gross_pnl_paise=gross,
            charges_paise=charges,
            net_pnl_paise=net,
            final_capital_paise=initial_capital_paise + net,
            return_bps=net * 10_000 // initial_capital_paise,
            win_rate=(wins / len(trades)) if trades else None,
            max_drawdown_paise=max_drawdown,
        ),
        tuple(curve),
    )


def run_backtest(
    definition: StrategyDefinition,
    bars_by_symbol: Mapping[str, tuple[Bar, ...]],
    *,
    start: datetime,
    end: datetime,
    initial_capital_paise: int,
    engine: BacktestEngineName = BacktestEngineName.FAST,
    charge_schedule: ChargeSchedule | None = None,
) -> BacktestResult:
    if start.utcoffset() is None or end.utcoffset() is None:
        raise BacktestError("Backtest bounds must be timezone-aware.")
    if end < start:
        raise BacktestError("Backtest end cannot precede its start.")
    if (
        isinstance(initial_capital_paise, bool)
        or not isinstance(initial_capital_paise, int)
        or initial_capital_paise <= 0
    ):
        raise BacktestError("Initial capital must be positive.")
    if not isinstance(engine, BacktestEngineName):
        raise BacktestError("Invalid backtest engine.")
    if not bars_by_symbol:
        raise BacktestError("At least one symbol of bars is required.")
    allocation = initial_capital_paise // max(1, len(definition.universe))
    trades: list[BacktestTrade] = []
    for symbol in definition.universe:
        symbol_bars = bars_by_symbol.get(symbol, ())
        if not symbol_bars:
            continue
        simulated = _simulate(
            definition,
            symbol_bars,
            start=start,
            end=end,
            allocation_paise=allocation,
            engine=engine,
            charge_schedule=charge_schedule,
        )
        trades.extend(
            BacktestTrade(
                symbol=symbol,
                entry_at=item.entry_at,
                exit_at=item.exit_at,
                entry_price_paise=item.entry_price_paise,
                exit_price_paise=item.exit_price_paise,
                quantity=item.quantity,
                gross_pnl_paise=item.gross_pnl_paise,
                charges_paise=item.charges_paise,
                net_pnl_paise=item.net_pnl_paise,
                exit_reason=item.exit_reason,
            )
            for item in simulated
        )
    ordered = tuple(sorted(trades, key=lambda item: (item.exit_at, item.symbol)))
    metrics, curve = _metrics(ordered, initial_capital_paise=initial_capital_paise)
    return BacktestResult(
        engine=engine,
        strategy_id=definition.strategy_id,
        version=definition.version,
        start=start,
        end=end,
        initial_capital_paise=initial_capital_paise,
        metrics=metrics,
        trades=ordered,
        equity_curve=curve,
    )


__all__ = [
    "BacktestEngineName",
    "BacktestError",
    "BacktestMetrics",
    "BacktestResult",
    "BacktestTrade",
    "run_backtest",
]
