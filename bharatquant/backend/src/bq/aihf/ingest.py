"""Populate the fundamentals the fund engine reads, for the names it trades.

The ingest itself is one Yahoo request per symbol (see
``bq/data/vendors/yahoo_fundamentals_vendor.py``). This module decides *which*
symbols, and is the one place that says so: the universe of every deployed paper
fund plus whatever names the owner asks for. Nothing here runs on a schedule by
itself — the ``aihf.fundamentals.ingest`` job calls it, and it is disabled by
default because it spends one network request per name.

Why the union and not "each fund ingests its own": fundamentals are slow-moving
and shared, so a name held by two funds is one fetch. It also means an owner can
warm the cache for a universe they are about to backtest before deploying a fund.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from bq.aihf.config import AiHfPaths, validate_name
from bq.aihf.engine.paper.deployed import load_deployed
from bq.aihf.engine.paper.tick import NothingDue
from bq.core import Clock, SystemClock
from bq.data.vendors.yahoo_fundamentals_vendor import (
    IngestReport,
    YahooFundamentalsIngest,
)

# One run's ceiling. Yahoo rate-limits, and a hundred names is already a
# minutes-long batch; a larger universe should be filled across several runs
# rather than in one call the scheduler would time out.
MAX_INGEST_SYMBOLS = 100


class IngestError(ValueError):
    """The requested universe cannot be ingested as asked."""


@dataclass(frozen=True, slots=True)
class IngestSummary:
    """What one run wrote. Counts and reasons only — never a payload."""

    reports: tuple[IngestReport, ...]
    symbols: tuple[str, ...]

    @property
    def snapshots(self) -> int:
        return sum(report.snapshots for report in self.reports)

    @property
    def earnings(self) -> int:
        return sum(report.earnings for report in self.reports)

    @property
    def failed(self) -> tuple[IngestReport, ...]:
        return tuple(report for report in self.reports if report.reason is not None)

    def to_dict(self) -> dict[str, object]:
        return {
            "symbols": len(self.symbols),
            "snapshots": self.snapshots,
            "earnings": self.earnings,
            "failures": [
                {"symbol": report.symbol, "reason": report.reason}
                for report in self.failed
            ],
        }


def deployed_universe(paths: AiHfPaths) -> tuple[str, ...]:
    """Every ticker the deployed paper funds trade, deduplicated, in order.

    A fund whose ``fund.yaml`` cannot be read is skipped rather than failing the
    whole ingest: one broken fund must not stop another fund's names from being
    refreshed. The service surfaces such a fund separately.
    """
    found: list[str] = []
    paper_root = paths.paper
    if not paper_root.is_dir():
        return ()
    for directory in sorted(paper_root.iterdir()):
        if not directory.is_dir():
            continue
        try:
            validate_name(directory.name, what="paper fund")
            deployed = load_deployed(directory)
        except (ValueError, NothingDue):
            continue
        for ticker in deployed.universe:
            cleaned = ticker.strip().upper()
            if cleaned and cleaned not in found:
                found.append(cleaned)
    return tuple(found)


def ingest_symbols(
    database_path: Path,
    symbols: Iterable[str],
    *,
    clock: Clock | None = None,
    transport: httpx.BaseTransport | None = None,
) -> IngestSummary:
    """Ingest every symbol, in order, and report what was written."""
    ordered = _normalize(symbols)
    ingestor = YahooFundamentalsIngest(
        database_path, clock=clock or SystemClock(), transport=transport
    )
    return IngestSummary(
        reports=tuple(ingestor.ingest(symbol) for symbol in ordered),
        symbols=ordered,
    )


def ingest_fund_universes(
    database_path: Path,
    paths: AiHfPaths,
    *,
    extra: Iterable[str] = (),
    clock: Clock | None = None,
    transport: httpx.BaseTransport | None = None,
) -> IngestSummary:
    """Top up every deployed fund's names, plus any the owner named."""
    symbols = list(deployed_universe(paths)) + list(extra)
    return ingest_symbols(database_path, symbols, clock=clock, transport=transport)


def _normalize(symbols: Iterable[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in symbols:
        ticker = str(value).strip().upper()
        if not ticker or ticker in cleaned:
            continue
        cleaned.append(ticker)
        if len(cleaned) > MAX_INGEST_SYMBOLS:
            raise IngestError(
                f"at most {MAX_INGEST_SYMBOLS} symbols per ingest run; "
                "ingest the rest in a second run"
            )
    return tuple(cleaned)


__all__ = [
    "IngestError",
    "IngestSummary",
    "MAX_INGEST_SYMBOLS",
    "deployed_universe",
    "ingest_fund_universes",
    "ingest_symbols",
]
