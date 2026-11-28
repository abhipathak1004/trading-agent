"""Market-data provenance contract for the Research Lab (ADR-054).

A frame is only comparable with another when its basis is known. This module
makes that basis a required part of the value: source, universe, currency, price
adjustment, the point-in-time ``as_of`` anchor, bar interval and the symbols that
were requested but not delivered.

Three rules are enforced here rather than documented as intentions:

* **Prices are exact paise.** Every OHLC column is ``Int64`` paise, matching
  :class:`bq.core.CurrencyPaise`. A float64 close would reintroduce the rounding
  drift that type exists to prevent, so the schema is refused if it drifts.
* **Missing is reported, never imputed.** ``missing_symbols`` must equal exactly
  the requested-universe minus delivered-symbols. Interior gaps are not forward
  filled; a caller that wants a gap acknowledged has to do it explicitly.
* **Adjusted and raw series never coexist.** A merged universe or benchmark load
  raises instead of silently producing a series that is partly back-adjusted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

import polars as pl

from bq.core import utc

CURRENCY_INR: Final = "INR"
SOURCE_LOCAL: Final = "local"
ADJUSTED: Final = "adjusted"
RAW: Final = "raw"
PRICE_ADJUSTMENTS: Final = frozenset({ADJUSTED, RAW})
BAR_INTERVALS: Final = frozenset({"1m", "5m", "15m", "60m", "1d"})

# Sources that reach the network. `source="local"` must never appear to have been
# served by one of these: an explicit Parquet/DuckDB read that quietly fell back
# to a vendor is the failure this guard exists for.
NETWORK_SOURCES: Final = frozenset({"yahoo", "kite", "fyers", "nse", "bse"})

PRICE_COLUMNS: Final = ("open", "high", "low", "close")


class ContractError(ValueError):
    """A frame cannot be described honestly, so it is refused."""


def expected_schema() -> dict[str, object]:
    return {
        "symbol": pl.String,
        "ts": pl.Datetime("us", "UTC"),
        "open": pl.Int64,
        "high": pl.Int64,
        "low": pl.Int64,
        "close": pl.Int64,
        "volume": pl.Int64,
    }


def _check_schema(frame: pl.DataFrame) -> None:
    expected = expected_schema()
    actual = dict(frame.schema)
    if actual != expected:
        raise ContractError(
            "A market-data frame must be "
            f"{sorted(expected)} with paise Int64 prices; got {sorted(actual)}."
        )


def _check_values(frame: pl.DataFrame) -> None:
    if frame.height == 0:
        return
    bad = frame.filter(
        (pl.col("open") <= 0)
        | (pl.col("high") <= 0)
        | (pl.col("low") <= 0)
        | (pl.col("close") <= 0)
        | (pl.col("volume") < 0)
    )
    if bad.height:
        raise ContractError("Prices must be positive paise and volume non-negative.")
    incoherent = frame.filter(
        (pl.col("high") < pl.col("low"))
        | (pl.col("high") < pl.col("open"))
        | (pl.col("high") < pl.col("close"))
        | (pl.col("low") > pl.col("open"))
        | (pl.col("low") > pl.col("close"))
    )
    if incoherent.height:
        raise ContractError(
            "OHLC bars are incoherent: high/low do not bracket the bar."
        )
    duplicates = frame.group_by(["symbol", "ts"]).len().filter(pl.col("len") > 1).height
    if duplicates:
        raise ContractError("A symbol may have at most one bar per timestamp.")


def _check_adjustment(price_adjustment: str) -> None:
    if price_adjustment not in PRICE_ADJUSTMENTS:
        raise ContractError("price_adjustment must be 'adjusted' or 'raw'.")


def assert_no_network_fallback(
    source: str, provenance_meta: Mapping[str, object]
) -> None:
    """An explicit local read must be terminal."""
    if source != SOURCE_LOCAL:
        return
    served_by = provenance_meta.get("served_by")
    if served_by is not None and str(served_by).lower() in NETWORK_SOURCES:
        raise ContractError(
            "source='local' cannot be served by a network vendor "
            f"({served_by}); an explicit local source must be terminal."
        )
    if provenance_meta.get("network_fallback"):
        raise ContractError(
            "An explicit local source must never fall back to the network."
        )


@dataclass(frozen=True, slots=True)
class MarketDataFrameContract:
    """A bar frame plus the basis needed to interpret it."""

    frame: pl.DataFrame
    source: str
    universe: tuple[str, ...]
    price_adjustment: str
    as_of_utc: datetime
    bar_interval: str
    missing_symbols: tuple[str, ...] = ()
    currency: str = CURRENCY_INR
    provenance_meta: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_schema(self.frame)
        _check_values(self.frame)
        _check_adjustment(self.price_adjustment)
        if self.currency != CURRENCY_INR:
            raise ContractError("BharatQuant frames are INR-only.")
        if self.bar_interval not in BAR_INTERVALS:
            raise ContractError(f"Unknown bar interval: {self.bar_interval}.")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ContractError("A frame needs a named source.")
        try:
            utc(self.as_of_utc)
        except ValueError as exc:
            raise ContractError("as_of_utc must be timezone-aware.") from exc
        assert_no_network_fallback(self.source, self.provenance_meta)
        if len(set(self.universe)) != len(self.universe):
            raise ContractError("The universe must not repeat a symbol.")
        delivered = set(self.frame.get_column("symbol").unique().to_list())
        if not delivered <= set(self.universe):
            raise ContractError("Every delivered symbol must be in the universe.")
        expected_missing = set(self.universe) - delivered
        if set(self.missing_symbols) != expected_missing:
            raise ContractError(
                "missing_symbols must be exactly the universe minus the delivered "
                f"symbols; expected {sorted(expected_missing)}, "
                f"got {sorted(set(self.missing_symbols))}."
            )
        if len(set(self.missing_symbols)) != len(self.missing_symbols):
            raise ContractError("missing_symbols must not repeat a symbol.")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.frame.get_column("symbol").unique().to_list()))

    @property
    def has_missing(self) -> bool:
        return bool(self.missing_symbols)

    def sorted_frame(self) -> pl.DataFrame:
        return self.frame.sort(["symbol", "ts"])

    def to_parquet(self, path: Path | str) -> None:
        """Write the deterministic row order the freeze digest is taken over."""
        self.sorted_frame().write_parquet(str(path))


def merge_contracts(
    contracts: Iterable[MarketDataFrameContract],
    *,
    universe: tuple[str, ...] | None = None,
) -> MarketDataFrameContract:
    """Combine frames that share one basis, refusing a mixed adjustment.

    This is the multi-symbol and benchmark load path: the one place a caller
    could accidentally concatenate a back-adjusted series with a raw one.
    """
    items = tuple(contracts)
    if not items:
        raise ContractError("Nothing to merge.")
    adjustments = {item.price_adjustment for item in items}
    if len(adjustments) > 1:
        raise ContractError(
            "Refusing to merge adjusted and raw price series into one frame: "
            f"{sorted(adjustments)}."
        )
    intervals = {item.bar_interval for item in items}
    if len(intervals) > 1:
        raise ContractError(f"Refusing to merge bar intervals: {sorted(intervals)}.")
    sources = {item.source for item in items}
    intervals_ok = next(iter(intervals))
    frames = [item.sorted_frame() for item in items]
    merged = pl.concat(frames).sort(["symbol", "ts"])
    merged_universe = (
        universe
        if universe is not None
        else tuple(sorted({symbol for item in items for symbol in item.universe}))
    )
    missing = tuple(
        sorted({symbol for item in items for symbol in item.missing_symbols})
    )
    return MarketDataFrameContract(
        frame=merged,
        source=next(iter(sources)) if len(sources) == 1 else "+".join(sorted(sources)),
        universe=merged_universe,
        price_adjustment=next(iter(adjustments)),
        as_of_utc=min(item.as_of_utc for item in items),
        bar_interval=intervals_ok,
        missing_symbols=missing,
        provenance_meta={"merged_from": sorted(sources)},
    )


__all__ = [
    "ADJUSTED",
    "BAR_INTERVALS",
    "CURRENCY_INR",
    "ContractError",
    "MarketDataFrameContract",
    "NETWORK_SOURCES",
    "PRICE_ADJUSTMENTS",
    "RAW",
    "SOURCE_LOCAL",
    "assert_no_network_fallback",
    "expected_schema",
    "merge_contracts",
]
