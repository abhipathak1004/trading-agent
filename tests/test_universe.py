"""A1 Universe agent tests — no network, synthetic bhavcopy in a temp cache."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from pinak.agents.universe import UniverseAgent
from pinak.core.config import UniverseConfig
from pinak.data.providers.nse_bhavcopy import BhavcopyError, NseBhavcopyProvider

END = date(2026, 11, 20)
CRORE = 1e7

SCHEMA = {
    "symbol": pl.Utf8,
    "series": pl.Utf8,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "turnover": pl.Float64,
    "delivery_pct": pl.Float64,
    "date": pl.Date,
}


def weekdays(end: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def bar(symbol: str, close: float, turnover_cr: float, day: date) -> dict[str, object]:
    return {
        "symbol": symbol,
        "series": "EQ",
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": 100_000.0,
        "turnover": turnover_cr * CRORE,
        "delivery_pct": 50.0,
        "date": day,
    }


def build(provider: NseBhavcopyProvider, days: list[date]) -> None:
    for index, day in enumerate(days):
        rows = [
            bar("BIG", 500.0, 50.0, day),
            bar("OK", 300.0, 20.0, day),
            bar("SMALL", 400.0, 1.0, day),
            bar("CHEAP", 10.0, 50.0, day),
            bar("LOWTURN", 250.0, 2.0 if index == len(days) - 1 else 50.0, day),
        ]
        if index >= len(days) - 5:
            rows.append(bar("THIN", 200.0, 30.0, day))
        pl.DataFrame(rows, schema=SCHEMA).write_parquet(provider.path_for(day))


@pytest.fixture()
def provider(tmp_path) -> NseBhavcopyProvider:
    return NseBhavcopyProvider(tmp_path / "bhavcopy")


def config(**overrides) -> UniverseConfig:
    defaults = {
        "lookback_days": 20,
        "min_price": 50.0,
        "min_adv_cr": 10.0,
        "min_turnover_cr": 5.0,
        "min_history_days": 15,
        "top_n": 150,
    }
    return UniverseConfig(**{**defaults, **overrides})


def test_tradable_set_is_exactly_the_eligible_symbols(provider):
    build(provider, weekdays(END, 20))
    result = UniverseAgent(provider, config()).run(END)

    assert set(result.tradable["symbol"].to_list()) == {"BIG", "OK"}
    assert result.as_of == END


def test_reject_reasons_are_specific(provider):
    build(provider, weekdays(END, 20))
    frame = UniverseAgent(provider, config()).run(END).frame
    reasons = dict(
        zip(frame["symbol"].to_list(), frame["reject_reasons"].to_list(), strict=True)
    )

    assert "illiquid_adv" in reasons["SMALL"]
    assert reasons["CHEAP"] == ["price_too_low"]
    assert reasons["LOWTURN"] == ["low_turnover"]
    assert reasons["THIN"] == ["short_history"]
    assert reasons["BIG"] == []


def test_ranking_is_by_traded_value(provider):
    build(provider, weekdays(END, 20))
    result = UniverseAgent(provider, config()).run(END)
    ranked = result.tradable.sort("rank")["symbol"].to_list()
    assert ranked == ["BIG", "OK"]


def test_top_n_selection(provider):
    build(provider, weekdays(END, 20))
    result = UniverseAgent(provider, config(top_n=1)).run(END)
    assert result.selected["symbol"].to_list() == ["BIG"]
    assert result.selected.height == 1


def test_excluded_symbols_are_rejected(provider):
    build(provider, weekdays(END, 20))
    result = UniverseAgent(provider, config(exclude_symbols=["big"])).run(END)
    assert set(result.tradable["symbol"].to_list()) == {"OK"}


def test_second_run_needs_no_network(provider):
    days = weekdays(END, 20)
    build(provider, days)

    def explode(_day):  # pragma: no cover - only fires on a cache miss
        raise AssertionError("network should not be touched on a warm cache")

    provider._download = explode  # type: ignore[method-assign]
    result = UniverseAgent(provider, config()).run(END)
    assert set(result.tradable["symbol"].to_list()) == {"BIG", "OK"}


def test_missing_day_is_treated_as_a_holiday(provider):
    days = weekdays(END, 25)
    build(provider, days)
    holiday = days[-5]
    provider.path_for(holiday).unlink()

    def absent(day):  # simulate a market holiday: no file anywhere
        raise BhavcopyError(f"no bhavcopy for {day}")

    provider._download = absent  # type: ignore[method-assign]

    agent = UniverseAgent(provider, config())
    resolved = agent._trading_days(END)

    assert len(resolved) == 20
    assert holiday not in resolved


def test_weekend_fetch_raises(provider):
    with pytest.raises(BhavcopyError):
        provider.fetch(date(2026, 11, 21))
