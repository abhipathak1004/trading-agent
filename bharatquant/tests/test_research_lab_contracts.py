"""The market-data provenance contract refuses dishonest frames."""

from datetime import UTC, datetime

import polars as pl
import pytest

from bq.research_lab.contracts import (
    ContractError,
    MarketDataFrameContract,
    merge_contracts,
)

AS_OF = datetime(2026, 1, 9, tzinfo=UTC)


def _frame(
    symbols: tuple[str, ...] = ("AAA",), prices: list[int] | None = None
) -> pl.DataFrame:
    sequence = prices or [10000, 10100, 10050]
    rows = [
        {
            "symbol": symbol,
            "ts": datetime(2026, 1, 5 + index, tzinfo=UTC),
            "open": price,
            "high": price + 50,
            "low": price - 50,
            "close": price,
            "volume": 1000,
        }
        for symbol in symbols
        for index, price in enumerate(sequence)
    ]
    return pl.DataFrame(rows, schema_overrides={"ts": pl.Datetime("us", "UTC")})


def _empty_frame() -> pl.DataFrame:
    """A schema-correct frame with no bars (polars cannot infer from zero rows)."""
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "ts": pl.Datetime("us", "UTC"),
            "open": pl.Int64,
            "high": pl.Int64,
            "low": pl.Int64,
            "close": pl.Int64,
            "volume": pl.Int64,
        }
    )


def _contract(
    *,
    symbols: tuple[str, ...] = ("AAA",),
    frame: pl.DataFrame | None = None,
    price_adjustment: str = "raw",
    source: str = "local",
    universe: tuple[str, ...] | None = None,
    missing: tuple[str, ...] = (),
) -> MarketDataFrameContract:
    return MarketDataFrameContract(
        frame=_frame(symbols) if frame is None else frame,
        source=source,
        universe=symbols if universe is None else universe,
        price_adjustment=price_adjustment,
        as_of_utc=AS_OF,
        bar_interval="1d",
        missing_symbols=missing,
    )


def test_valid_contract_reports_its_basis() -> None:
    contract = _contract(symbols=("AAA", "BBB"))
    assert contract.symbols == ("AAA", "BBB")
    assert contract.currency == "INR"
    assert contract.price_adjustment == "raw"
    assert contract.has_missing is False


def test_float_prices_are_refused() -> None:
    drifted = _frame().with_columns(pl.col("close").cast(pl.Float64))
    with pytest.raises(ContractError, match="paise Int64"):
        _contract(frame=drifted)


def test_missing_symbols_must_be_exactly_the_difference() -> None:
    with pytest.raises(ContractError, match="missing_symbols"):
        _contract(universe=("AAA", "CCC"), missing=())
    # Being explicit is accepted.
    contract = _contract(universe=("AAA", "CCC"), missing=("CCC",))
    assert contract.missing_symbols == ("CCC",)
    assert contract.has_missing is True


def test_delivered_symbol_outside_universe_is_refused() -> None:
    with pytest.raises(ContractError, match="in the universe"):
        _contract(symbols=("AAA",), universe=("BBB",), missing=("BBB",))


def test_incoherent_ohlc_is_refused() -> None:
    broken = _frame().with_columns((pl.col("low") + 1000).alias("low"))
    with pytest.raises(ContractError, match="incoherent"):
        _contract(frame=broken)


def test_duplicate_bar_is_refused() -> None:
    doubled = pl.concat([_frame(), _frame()]).sort(["symbol", "ts"])
    with pytest.raises(ContractError, match="one bar per timestamp"):
        _contract(frame=doubled)


def test_unknown_adjustment_and_currency_are_refused() -> None:
    with pytest.raises(ContractError, match="price_adjustment"):
        _contract(price_adjustment="split-adjusted")
    with pytest.raises(ContractError, match="INR-only"):
        MarketDataFrameContract(
            frame=_frame(),
            source="local",
            universe=("AAA",),
            price_adjustment="raw",
            as_of_utc=AS_OF,
            bar_interval="1d",
            currency="USD",
        )


def test_local_source_cannot_be_served_by_the_network() -> None:
    with pytest.raises(ContractError, match="terminal"):
        MarketDataFrameContract(
            frame=_frame(),
            source="local",
            universe=("AAA",),
            price_adjustment="raw",
            as_of_utc=AS_OF,
            bar_interval="1d",
            provenance_meta={"served_by": "yahoo"},
        )
    with pytest.raises(ContractError, match="never fall back"):
        MarketDataFrameContract(
            frame=_frame(),
            source="local",
            universe=("AAA",),
            price_adjustment="raw",
            as_of_utc=AS_OF,
            bar_interval="1d",
            provenance_meta={"network_fallback": True},
        )


def test_naive_as_of_is_refused() -> None:
    with pytest.raises(ContractError, match="timezone-aware"):
        MarketDataFrameContract(
            frame=_frame(),
            source="local",
            universe=("AAA",),
            price_adjustment="raw",
            as_of_utc=datetime(2026, 1, 9),
            bar_interval="1d",
        )


def test_merge_refuses_mixed_adjustment_and_accepts_a_shared_basis() -> None:
    raw = _contract(symbols=("AAA",))
    adjusted = _contract(symbols=("BBB",), price_adjustment="adjusted")
    with pytest.raises(ContractError, match="adjusted and raw"):
        merge_contracts([raw, adjusted])

    merged = merge_contracts([raw, _contract(symbols=("BBB",))])
    assert merged.symbols == ("AAA", "BBB")
    assert merged.source == "local"


def test_merge_carries_missing_symbols_forward() -> None:
    present = _contract(symbols=("AAA",))
    absent = _contract(frame=_empty_frame(), universe=("CCC",), missing=("CCC",))
    merged = merge_contracts([present, absent])
    assert merged.symbols == ("AAA",)
    assert merged.missing_symbols == ("CCC",)
