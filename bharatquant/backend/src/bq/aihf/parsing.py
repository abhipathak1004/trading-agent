"""Read-only parsing of what ``aihf`` writes.

Every value here comes from a file aihf produced (a ``SessionRecord``, a
``FundBacktestResult``, a mandate YAML) or from its stdout. Nothing is
recomputed: the equity curve, the risk clamps and the conviction numbers are
aihf's, exactly as it serialised them.

Two rules, both about not trusting a subprocess blindly:

* Fields are **bounded** (see the ``MAX_*`` constants) so a runaway record
  cannot become a huge response or a huge browser payload.
* Optional/derived fields are **not** invented. aihf omits ``allocation``,
  ``drawdown`` and ``exposures`` when they do not apply, and that absence is
  meaningful — a clamp that did not fire must not render as one that did.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

MAX_REASONING_CHARS = 2_000
MAX_TICKERS = 60
MAX_STRATEGIES = 16
MAX_SESSIONS = 2_000
MAX_TRADES = 5_000

SIDES = ("buy", "sell")

# aihf's seven shrinking stages (risk/limits.py) plus the drawdown stop-out.
RISK_LIMITS = (
    "max_position_pct",
    "max_gross_exposure",
    "max_net_exposure",
    "max_drawdown_pct",
    "halve_drawdown_pct",
    "max_adv_pct",
    "max_beta",
    "max_sector_pct",
    "max_volatility",
)


class AiHfParseError(ValueError):
    """A record aihf wrote is not the shape we understand."""


def _record(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AiHfParseError("Expected a JSON object.")
    return value


def _list(value: object) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AiHfParseError("Expected a JSON array.")
    return value


def _text(value: object, *, limit: int = 200, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return value[:limit]


def _reasoning(value: object) -> str | None:
    text = _text(value, limit=MAX_REASONING_CHARS).strip()
    return text or None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _float_map(value: object, *, limit: int = MAX_TICKERS) -> dict[str, float]:
    result: dict[str, float] = {}
    if not isinstance(value, Mapping):
        return result
    for ticker, weight in value.items():
        number = _number(weight)
        if number is not None:
            result[_text(ticker, limit=16)] = number
        if len(result) >= limit:
            break
    return result


def parse_universe(value: object) -> list[str]:
    return [_text(item, limit=16) for item in _list(value)[:MAX_TICKERS] if _text(item)]


# ---------------------------------------------------------------------------
# signals / clamps
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalView:
    """One analyst's view at a point in time."""

    model_name: str
    ticker: str
    date: str
    value: float
    reasoning: str | None

    @property
    def side(self) -> str:
        if self.value > 0:
            return "long"
        if self.value < 0:
            return "short"
        return "neutral"


@dataclass(frozen=True, slots=True)
class ClampView:
    """One deterministic risk limit firing. ``before``/``after`` are aihf's."""

    limit: str
    ticker: str | None
    sector: str | None
    before: float
    after: float


@dataclass(frozen=True, slots=True)
class StrategyView:
    name: str
    slice: float
    signals: tuple[SignalView, ...]
    final_contribution: dict[str, float]
    flat_reason: str | None


def parse_signal(value: object) -> SignalView:
    record = _record(value)
    number = _number(record.get("value"))
    if number is None:
        raise AiHfParseError("A signal must carry a numeric value.")
    return SignalView(
        model_name=_text(record.get("model_name"), limit=64, default="unknown"),
        ticker=_text(record.get("ticker"), limit=16, default="?"),
        date=_text(record.get("date"), limit=10),
        value=max(-1.0, min(1.0, number)),
        reasoning=_reasoning(record.get("reasoning")),
    )


def parse_clamp(value: object) -> ClampView:
    record = _record(value)
    before = _number(record.get("before"))
    after = _number(record.get("after"))
    if before is None or after is None:
        raise AiHfParseError("A clamp must carry numeric before/after values.")
    ticker = record.get("ticker")
    sector = record.get("sector")
    return ClampView(
        limit=_text(record.get("limit"), limit=64, default="unknown"),
        ticker=_text(ticker, limit=16) or None,
        sector=_text(sector, limit=64) or None,
        before=before,
        after=after,
    )


def parse_strategy(value: object) -> StrategyView:
    record = _record(value)
    return StrategyView(
        name=_text(record.get("name"), limit=64, default="strategy"),
        slice=_number(record.get("slice")) or 0.0,
        signals=tuple(parse_signal(item) for item in _list(record.get("signals"))),
        final_contribution=_float_map(record.get("final_contribution")),
        flat_reason=_text(record.get("flat_reason"), limit=200) or None,
    )


def _clamps(value: object) -> tuple[ClampView, ...]:
    return tuple(parse_clamp(item) for item in _list(value))


def _strategies(value: object) -> tuple[StrategyView, ...]:
    entries = _list(value)[:MAX_STRATEGIES]
    return tuple(parse_strategy(item) for item in entries)


# ---------------------------------------------------------------------------
# orders / fills / execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderView:
    ticker: str
    side: str
    quantity: int
    price: float

    @property
    def notional(self) -> float:
        return self.quantity * self.price


@dataclass(frozen=True, slots=True)
class FillView:
    ticker: str
    side: str
    quantity: int
    price: float


def parse_order(value: object) -> OrderView:
    record = _record(value)
    quantity = _count(record.get("quantity"))
    price = _number(record.get("price"))
    side = _text(record.get("side"), limit=8)
    if quantity is None or price is None or side not in SIDES:
        raise AiHfParseError("An order needs a side, a quantity and a price.")
    return OrderView(
        ticker=_text(record.get("ticker"), limit=16, default="?"),
        side=side,
        quantity=quantity,
        price=price,
    )


def parse_fill(value: object) -> FillView:
    record = _record(value)
    quantity = _count(record.get("quantity"))
    price = _number(record.get("price"))
    side = _text(record.get("side"), limit=8)
    if quantity is None or price is None or side not in SIDES:
        raise AiHfParseError("A fill needs a side, a quantity and a price.")
    return FillView(
        ticker=_text(record.get("ticker"), limit=16, default="?"),
        side=side,
        quantity=quantity,
        price=price,
    )


@dataclass(frozen=True, slots=True)
class ExecutionView:
    """A decision that was executed: what was ordered, filled, and the book after."""

    as_of: str
    orders: tuple[OrderView, ...]
    fills: tuple[FillView, ...]
    positions: dict[str, int]
    cash: float
    nav: float
    clamps: tuple[ClampView, ...]
    slipped_from_signal: bool

    @property
    def traded_notional(self) -> float:
        return sum(fill.quantity * fill.price for fill in self.fills)


def _int_map(value: object, *, limit: int = MAX_TICKERS) -> dict[str, int]:
    result: dict[str, int] = {}
    if not isinstance(value, Mapping):
        return result
    for ticker, shares in value.items():
        number = _count(shares)
        if number is not None:
            result[_text(ticker, limit=16)] = number
        if len(result) >= limit:
            break
    return result


def parse_execution(value: object) -> ExecutionView:
    record = _record(value)
    return ExecutionView(
        as_of=_text(record.get("as_of"), limit=10),
        orders=tuple(
            parse_order(item) for item in _list(record.get("orders"))[:MAX_TRADES]
        ),
        fills=tuple(
            parse_fill(item) for item in _list(record.get("fills"))[:MAX_TRADES]
        ),
        positions=_int_map(record.get("positions")),
        cash=_number(record.get("cash")) or 0.0,
        nav=_number(record.get("nav")) or 0.0,
        clamps=_clamps(record.get("clamps")),
        # aihf executes a decision at the next close; the marks may have moved,
        # so the order prices are the reference at decision time, not the fill.
        slipped_from_signal=bool(_list(record.get("fills"))),
    )


# ---------------------------------------------------------------------------
# decisions / session records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecisionView:
    """An assessment with no orders: what was seen, said, and targeted."""

    as_of: str
    universe: list[str]
    strategies: tuple[StrategyView, ...]
    clamps: tuple[ClampView, ...]
    target_weights: dict[str, float]
    final_weights: dict[str, float]
    drawdown: float | None
    is_stopped_out: bool
    is_halved: bool

    @property
    def signal_count(self) -> int:
        return sum(len(strategy.signals) for strategy in self.strategies)


def parse_decision(value: object) -> DecisionView:
    record = _record(value)
    clamps = _clamps(record.get("clamps"))
    positions = _float_map(record.get("final_weights"))
    return DecisionView(
        as_of=_text(record.get("as_of"), limit=10),
        universe=parse_universe(record.get("universe")),
        strategies=_strategies(record.get("strategies")),
        clamps=clamps,
        target_weights=_float_map(record.get("target_weights")),
        final_weights=positions,
        drawdown=_number(record.get("drawdown")),
        is_stopped_out=any(clamp.limit == "max_drawdown_pct" for clamp in clamps),
        is_halved=any(clamp.limit == "halve_drawdown_pct" for clamp in clamps),
    )


@dataclass(frozen=True, slots=True)
class SessionView:
    """One session of a fund, as it sits in the hash-chained ledger."""

    session: str
    fund: str
    universe: list[str]
    nav: float
    cash: float
    positions: dict[str, int]
    marks: dict[str, float]
    benchmark: str
    benchmark_close: float
    rebalance: bool
    prev_hash: str | None
    hash: str
    llm_model: str | None
    executed: ExecutionView | None
    decision: DecisionView | None
    # aihf's own JSON, untouched. An approved preview adopts the sandbox
    # directory rather than re-serialising from the bounded view, but keeping
    # the original makes that intent inspectable and testable.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def drawdown(self) -> float | None:
        return None if self.decision is None else self.decision.drawdown


def parse_session(value: object) -> SessionView:
    record = _record(value)
    session = _text(record.get("session"), limit=32)
    if not session:
        raise AiHfParseError("A session record must name its session.")
    executed = record.get("executed")
    decision = record.get("decision")
    return SessionView(
        session=session,
        fund=_text(record.get("fund"), limit=64),
        universe=parse_universe(record.get("universe")),
        nav=_number(record.get("nav")) or 0.0,
        cash=_number(record.get("cash")) or 0.0,
        positions=_int_map(record.get("positions")),
        marks=_float_map(record.get("marks")),
        benchmark=_text(record.get("benchmark"), limit=16, default="SPY"),
        benchmark_close=_number(record.get("benchmark_close")) or 0.0,
        rebalance=bool(record.get("rebalance")),
        prev_hash=_text(record.get("prev_hash"), limit=64) or None,
        hash=_text(record.get("hash"), limit=64),
        llm_model=_text(record.get("llm_model"), limit=64) or None,
        executed=None if executed is None else parse_execution(executed),
        decision=None if decision is None else parse_decision(decision),
        raw=dict(record),
    )


def parse_session_document(text: str) -> SessionView:
    """One ``aihf paper tick`` stdout payload."""
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise AiHfParseError(
            f"aihf did not print a JSON session record: {exc}"
        ) from exc
    return parse_session(parsed)


# ---------------------------------------------------------------------------
# backtest results (research/*.json)
# ---------------------------------------------------------------------------

METRIC_KEYS = (
    "total_return_pct",
    "annualized_return_pct",
    "sharpe_ratio",
    "max_drawdown_pct",
    "benchmark_return_pct",
    "excess_return_pct",
    "n_cycles",
    "n_orders",
    "traded_notional",
    "trading_cost",
)


@dataclass(frozen=True, slots=True)
class BacktestMetricsView:
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    benchmark_return_pct: float
    excess_return_pct: float
    n_cycles: int
    n_orders: int
    traded_notional: float
    trading_cost: float


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    """The identity and headline stats of a saved result (no curve, no records)."""

    path: str
    fund: str
    universe: list[str]
    start: str
    end: str
    benchmark: str
    rebalance: str
    capital: float
    metrics: BacktestMetricsView


@dataclass(frozen=True, slots=True)
class BacktestDetail:
    summary: BacktestSummary
    dates: list[str]
    nav: list[float]
    benchmark_nav: list[float]
    sessions: tuple[SessionView, ...]


def parse_metrics(value: object) -> BacktestMetricsView:
    record = _record(value)
    numbers = {key: _number(record.get(key)) for key in METRIC_KEYS}
    missing = [key for key, number in numbers.items() if number is None]
    if missing:
        raise AiHfParseError(
            f"A backtest result is missing metrics: {', '.join(missing)}"
        )
    counts = {key: _count(record.get(key)) or 0 for key in ("n_cycles", "n_orders")}
    return BacktestMetricsView(
        total_return_pct=float(numbers["total_return_pct"] or 0.0),
        annualized_return_pct=float(numbers["annualized_return_pct"] or 0.0),
        sharpe_ratio=float(numbers["sharpe_ratio"] or 0.0),
        max_drawdown_pct=float(numbers["max_drawdown_pct"] or 0.0),
        benchmark_return_pct=float(numbers["benchmark_return_pct"] or 0.0),
        excess_return_pct=float(numbers["excess_return_pct"] or 0.0),
        n_cycles=counts["n_cycles"],
        n_orders=counts["n_orders"],
        traded_notional=float(numbers["traded_notional"] or 0.0),
        trading_cost=float(numbers["trading_cost"] or 0.0),
    )


def parse_backtest_summary(value: object, *, path: str = "") -> BacktestSummary:
    record = _record(value)
    metrics = record.get("metrics")
    if metrics is None:
        raise AiHfParseError("This file is not a backtest result (no metrics).")
    return BacktestSummary(
        path=path,
        fund=_text(record.get("fund"), limit=64),
        universe=parse_universe(record.get("universe")),
        start=_text(record.get("start"), limit=10),
        end=_text(record.get("end"), limit=10),
        benchmark=_text(record.get("benchmark"), limit=16, default="SPY"),
        rebalance=_text(record.get("rebalance"), limit=64),
        capital=_number(record.get("capital")) or 0.0,
        metrics=parse_metrics(metrics),
    )


def _paired_series(dates: Sequence[Any], values: Sequence[Any]) -> list[float]:
    if len(dates) != len(values):
        raise AiHfParseError(
            "An equity curve's dates and values must be the same length."
        )
    points: list[float] = []
    for value in values[: len(dates)]:
        number = _number(value)
        if number is None:
            raise AiHfParseError("An equity curve must be numeric.")
        points.append(number)
    return points


def parse_backtest_detail(value: object, *, path: str = "") -> BacktestDetail:
    record = _record(value)
    summary = parse_backtest_summary(record, path=path)
    dates = [
        _text(item, limit=32) for item in _list(record.get("dates"))[:MAX_SESSIONS]
    ]
    nav = _paired_series(dates, _list(record.get("nav")))
    benchmark = _paired_series(dates, _list(record.get("benchmark_nav")))
    records = _list(record.get("records"))
    sessions = tuple(parse_session(item) for item in records[:MAX_SESSIONS])
    return BacktestDetail(
        summary=summary,
        dates=dates,
        nav=nav,
        benchmark_nav=benchmark,
        sessions=sessions,
    )


def trade_rows(detail: BacktestDetail) -> list[dict[str, object]]:
    """The signal-date -> execution-date trade log, from aihf's own records.

    A decision made at session *T* is executed at *T+1*; the execution record
    carries both, so the pair is never inferred from list order.
    """
    rows: list[dict[str, object]] = []
    for session in detail.sessions:
        executed = session.executed
        if executed is None:
            continue
        for order, fill in zip(executed.orders, executed.fills, strict=False):
            rows.append(
                {
                    "signal_date": _decision_date(detail, session.session),
                    "execution_date": session.session,
                    "ticker": order.ticker,
                    "side": order.side,
                    "quantity": order.quantity,
                    "price": fill.price,
                    "reference_price": order.price,
                    "cost": round(abs(fill.price - order.price) * order.quantity, 6),
                }
            )
        if len(rows) >= MAX_TRADES:
            break
    return rows


def _decision_date(detail: BacktestDetail, session: str) -> str:
    """The session whose decision produced this execution."""
    for index, current in enumerate(detail.sessions):
        if current.session == session and index > 0:
            return detail.sessions[index - 1].session
    return ""


def iter_reasoning(detail: BacktestDetail) -> Iterable[tuple[str, SignalView]]:
    """Every ``(session, signal)`` pair that carries a rationale."""
    for session in detail.sessions:
        decision = session.decision
        if decision is None:
            continue
        for strategy in decision.strategies:
            for signal in strategy.signals:
                if signal.reasoning:
                    yield session.session, signal


@dataclass(frozen=True, slots=True)
class LedgerView:
    """A fund's chain, as aihf recorded it."""

    sessions: tuple[SessionView, ...]
    chain_verified: bool
    problem: str | None = field(default=None)

    @property
    def head(self) -> str | None:
        return self.sessions[-1].hash if self.sessions else None
