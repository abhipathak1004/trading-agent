"""The session market layer: IST dates, NSE holidays, and Indian delivery costs.

These are the four facts that decide *when* a fund may look at the market and
*what it pays* to trade it. Getting either wrong is silent: a wrong date makes a
fund a day stale, a wrong fee makes every backtest optimistic. So each is pinned
here rather than left to a live run to reveal.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from bq.aihf.bridge import calendar as market
from bq.aihf.bridge.clock import NseDailyClock
from bq.aihf.bridge.costs import indian_delivery_costs
from bq.aihf.engine.backtesting.fund import FundBacktestResult
from bq.aihf.engine.fund import load_spec
from bq.paper_charges import compute_charges

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Completed sessions
# ---------------------------------------------------------------------------


def test_after_the_nse_close_today_counts() -> None:
    """A 20:00 IST run has today's close; returning yesterday would make every
    evening run a day stale, which is the bug this function exists to avoid."""
    after = datetime(2026, 11, 25, 20, 0, tzinfo=IST)  # a Wednesday
    assert market.completed_through(after) == "2026-11-25"


def test_before_the_nse_close_yesterday_is_the_last_complete_session() -> None:
    before = datetime(2026, 11, 25, 10, 0, tzinfo=IST)  # Wednesday, open
    assert market.completed_through(before) == "2026-11-24"


def test_a_saturday_resolves_to_friday() -> None:
    saturday = datetime(2026, 11, 28, 12, 0, tzinfo=IST)
    assert market.completed_through(saturday) == "2026-11-27"


def test_fixed_market_holidays_are_never_a_session() -> None:
    """Republic Day is a fixed exchange holiday. On the day itself, a run must
    resolve to the previous trading day, not to the holiday."""
    republic_day = datetime(2026, 1, 26, 20, 0, tzinfo=IST)
    assert market.completed_through(republic_day) == "2026-01-23"  # Friday


def test_previous_day_skips_a_holiday_cluster() -> None:
    """26 Jan 2026 is a Monday holiday: the prior trading date is Friday 23rd,
    not Sunday 25th nor the holiday itself."""
    assert market.previous_day("2026-01-26") == "2026-01-23"


def test_previous_day_walks_back_over_a_weekend() -> None:
    assert market.previous_day("2026-11-30") == "2026-11-27"  # Monday -> Friday


# ---------------------------------------------------------------------------
# The clock the engine runs on
# ---------------------------------------------------------------------------


class _FakeData:
    """Returns one daily bar per weekday in the requested window, at a fixed close."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def get_prices(self, ticker: str, start_date: str, end_date: str, **_):
        from datetime import date, timedelta

        from bq.aihf.engine.data.models import Price

        self.calls.append((ticker, start_date, end_date))
        bars = []
        day = date.fromisoformat(start_date[:10])
        last = date.fromisoformat(end_date[:10])
        while day <= last:
            if day.weekday() < 5:
                bars.append(
                    Price(
                        open=99.0,
                        close=100.0,
                        high=101.0,
                        low=98.0,
                        volume=1000,
                        time=f"{day.isoformat()}T15:30:00",
                    )
                )
            day += timedelta(days=1)
        return bars


def test_the_clock_rejects_an_intraday_session_instead_of_mispricing_it() -> None:
    """A daily clock valued at a bar's close would silently price every minute
    fund as a daily one. Reject the shape instead."""
    clock = NseDailyClock(_FakeData())
    with pytest.raises(market.MarketError, match="intraday"):
        clock.is_complete("2026-11-25T10:05:00")


def test_the_clock_marks_a_name_at_its_exact_session_close() -> None:
    clock = NseDailyClock(_FakeData())
    marked = clock.marks(["RELIANCE"], "2026-11-25")
    assert marked.prices == {"RELIANCE": 100.0}


def test_the_clock_refuses_to_mark_without_a_close() -> None:
    """A missing close must raise: valuing a book at zero (or a stale close)
    would make a backtest quietly wrong instead of loudly broken."""

    class _Empty:
        def get_prices(self, *_args, **_kwargs):
            return []

    clock = NseDailyClock(_Empty())
    with pytest.raises(market.MarketError, match="no close"):
        clock.marks(["RELIANCE"], "2026-11-25")


# ---------------------------------------------------------------------------
# Indian delivery costs
# ---------------------------------------------------------------------------


def test_delivery_charges_match_the_paper_ledger_to_the_paise() -> None:
    """One schedule for the whole app: a fund must not charge differently from
    the paper-trading ledger it settles against."""
    costs = indian_delivery_costs()
    for side, product in (("buy", "BUY"), ("sell", "SELL")):
        engine_fee = costs.charge(side, 100, 1000.0)
        reference = compute_charges(
            side=product, product="CAS", quantity=100, price_paise=100_000
        )
        assert engine_fee == pytest.approx(reference.total_paise / 100)


def test_a_delivery_buy_pays_stt_and_stamp_but_a_sell_pays_no_stamp() -> None:
    costs = indian_delivery_costs()
    buy = costs.charge("buy", 100, 1000.0)
    sell = costs.charge("sell", 100, 1000.0)
    # Buy pays stamp duty (0.015%); sell does not, so buy is dearer.
    assert buy > sell


def test_no_per_share_commission_and_no_impact_by_default() -> None:
    """The US defaults ($0.005/share, square-root impact) do not exist for an
    NSE delivery trade and would overcharge every fill."""
    costs = indian_delivery_costs()
    assert costs.commission(500) == 0.0
    assert costs.impact(participation=0.1, volatility=0.02) == 0.0


def test_half_spread_survives_as_the_one_slippage_term() -> None:
    costs = indian_delivery_costs(half_spread_bps=5.0)
    assert costs.fill_price("buy", 1000.0) == pytest.approx(1000.5)
    assert costs.fill_price("sell", 1000.0) == pytest.approx(999.5)


def test_costs_serialize_without_the_pinned_rate_schedule() -> None:
    """The mandate is hashed into the ledger. A test-only rate override must not
    leak into that hash."""
    from bq.paper_charges import ChargeSchedule

    costs = indian_delivery_costs(schedule=ChargeSchedule.zero())
    payload = costs.model_dump()
    assert "rate_schedule" not in payload
    assert "_rate_schedule" not in payload


# ---------------------------------------------------------------------------
# Mandate validation: the shapes this pass does not run
# ---------------------------------------------------------------------------


def _mandate(tmp_path, **overrides) -> str:

    base = {
        "schema_version": 2,
        "name": "test-fund",
        "strategies": [
            {
                "name": "quant",
                "blend": {"mode": "long_only"},
                "models": [{"name": "pead"}],
            }
        ],
        "risk": {"max_position_pct": 0.25, "max_gross_exposure": 1.0},
        "capital": 100000,
        "rebalance": "weekly",
        "benchmark": "NIFTY 50",
    }
    base.update(overrides)
    path = tmp_path / "mandate.yaml"
    import yaml

    path.write_text(yaml.safe_dump(base, sort_keys=False))
    return str(path)


def test_a_minute_mandate_is_refused_at_parse_time(tmp_path) -> None:
    path = _mandate(tmp_path, rebalance="minute", interval=5.0)
    with pytest.raises(ValueError, match="minute funds are not supported"):
        load_spec(path)


def test_a_universe_rule_is_refused_at_parse_time(tmp_path) -> None:
    """The engine's own validator fires first when no analyst declares screening
    factors; either way a `universe:` mandate is refused before it runs. Assert
    on the outcome (a ValueError naming the field) rather than one message."""
    path = _mandate(
        tmp_path,
        universe={"filters": [{"field": "market_cap", "operator": "gt", "value": 1e9}]},
    )
    with pytest.raises(ValueError) as raised:
        load_spec(path)
    assert "universe" in str(raised.value).lower()


def test_a_weekly_pead_mandate_loads() -> None:
    """The shipped example is the shape an owner starts from; it must parse."""
    from pathlib import Path

    example = Path(__file__).parents[1] / "backend/src/bq/aihf/engine/fund/example.yaml"
    spec = load_spec(example)
    assert spec.rebalance == "weekly"
    assert spec.benchmark == "NIFTY 50"
    assert not spec.is_intraday
    assert spec.universe is None


def test_the_benchmark_defaults_to_the_nifty() -> None:
    """A US default would build the trading grid from `SPY`, which this app
    cannot resolve — every backtest would fail on its first fetch."""
    from bq.aihf.engine.fund.spec import FundSpec

    data = {
        "schema_version": 2,
        "name": "f",
        "strategies": [
            {"name": "q", "blend": {"mode": "long_only"}, "models": [{"name": "pead"}]}
        ],
        "risk": {"max_position_pct": 0.2, "max_gross_exposure": 1.0},
        "capital": 100000.0,
    }
    assert FundSpec.model_validate(data).benchmark == "NIFTY 50"


def test_backtest_result_is_unaffected_by_the_cost_bridge() -> None:
    """Importing the result type must not require a data client or a market:
    the bridge only supplies what a run is handed."""
    assert FundBacktestResult.__name__ == "FundBacktestResult"


def test_charge_price_conversion_refuses_a_sub_paisa_price() -> None:
    from bq.aihf.bridge.costs import _price_paise

    with pytest.raises(ValueError, match="at least one paisa"):
        _price_paise(0.001)


def test_charge_price_conversion_is_exact_paise() -> None:
    from bq.aihf.bridge.costs import _price_paise

    assert _price_paise(1000.0) == 100_000
    assert _price_paise(Decimal("1234.567")) == 123_457


# ---------------------------------------------------------------------------
# The engine actually uses the installed market clock
# ---------------------------------------------------------------------------


def test_the_installed_factory_is_what_the_engine_uses() -> None:
    """`clock_for` is called deep inside `advance`/`assess_fund` with no clock.
    If the app's factory were not installed, a fund would silently run on New
    York dates — the defect would be invisible until a session was a day off.
    """
    from bq.aihf.bridge.clock import nse_clock_factory
    from bq.aihf.engine.fund.spec import FundSpec
    from bq.aihf.engine.pipeline import clock as engine_clock

    spec = FundSpec.model_validate(
        {
            "schema_version": 2,
            "name": "f",
            "strategies": [
                {
                    "name": "q",
                    "blend": {"mode": "long_only"},
                    "models": [{"name": "pead"}],
                }
            ],
            "risk": {"max_position_pct": 0.2, "max_gross_exposure": 1.0},
            "capital": 100000.0,
        }
    )
    original = engine_clock._CLOCK_FACTORY
    try:
        engine_clock.set_clock_factory(nse_clock_factory())
        assert isinstance(engine_clock.clock_for(spec, _FakeData()), NseDailyClock)
    finally:
        engine_clock.set_clock_factory(original)


def test_without_a_factory_the_engine_keeps_its_own_clock() -> None:
    """The vendored default must stay untouched, so upstream's own tests and a
    bare `backtest_fund` in a unit test behave exactly as written."""
    from bq.aihf.engine.fund.spec import FundSpec
    from bq.aihf.engine.pipeline import clock as engine_clock

    spec = FundSpec.model_validate(
        {
            "schema_version": 2,
            "name": "f",
            "strategies": [
                {
                    "name": "q",
                    "blend": {"mode": "long_only"},
                    "models": [{"name": "pead"}],
                }
            ],
            "risk": {"max_position_pct": 0.2, "max_gross_exposure": 1.0},
            "capital": 100000.0,
        }
    )
    original = engine_clock._CLOCK_FACTORY
    try:
        engine_clock.set_clock_factory(None)
        assert isinstance(
            engine_clock.clock_for(spec, _FakeData()), engine_clock.DailyClock
        )
    finally:
        engine_clock.set_clock_factory(original)


def test_the_factory_refuses_an_intraday_mandate() -> None:
    from bq.aihf.bridge.clock import nse_clock_factory

    class _Spec:
        is_intraday = True

    with pytest.raises(market.MarketError, match="intraday"):
        nse_clock_factory()(_Spec(), _FakeData(), False)
