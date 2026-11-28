"""Which symbols an ingest run covers.

The rule is the union of every deployed fund's tickers. Getting it wrong is
cheap to miss and awkward to notice: a fund whose names are never ingested has
every persona abstain with `InsufficientData`, which looks like a cautious model
rather than a missing feed.
"""

from __future__ import annotations

import pytest

from bq.aihf.config import resolve_paths
from bq.aihf.engine.fund.spec import FundSpec
from bq.aihf.engine.paper.deployed import deploy
from bq.aihf.ingest import (
    MAX_INGEST_SYMBOLS,
    IngestError,
    deployed_universe,
    ingest_symbols,
)
from bq.infra.database import migrate


def _spec(name: str = "f") -> FundSpec:
    return FundSpec.model_validate(
        {
            "schema_version": 2,
            "name": name,
            "strategies": [
                {
                    "name": "q",
                    "blend": {"mode": "long_only"},
                    "models": [{"name": "pead"}],
                }
            ],
            "risk": {"max_position_pct": 0.25, "max_gross_exposure": 1.0},
            "capital": 100000.0,
        }
    )


@pytest.fixture
def paths(tmp_path):
    db = tmp_path / "bq.db"
    migrate(db, create=True)
    return resolve_paths(database_path=db)


def test_no_funds_means_no_symbols(paths) -> None:
    assert deployed_universe(paths) == ()


def test_a_fund_contributes_its_universe(paths) -> None:
    paths.paper.mkdir(parents=True, exist_ok=True)
    deploy("alpha", _spec("alpha"), ["RELIANCE", "TCS"], root=paths.paper)
    assert deployed_universe(paths) == ("RELIANCE", "TCS")


def test_two_funds_sharing_a_name_fetch_it_once(paths) -> None:
    """Fundamentals are shared, slow-moving data: a name in two funds is one
    network request, not two."""
    paths.paper.mkdir(parents=True, exist_ok=True)
    deploy("alpha", _spec("alpha"), ["RELIANCE", "TCS"], root=paths.paper)
    deploy("beta", _spec("beta"), ["TCS", "INFY"], root=paths.paper)
    assert deployed_universe(paths) == ("RELIANCE", "TCS", "INFY")


def test_a_broken_fund_does_not_hide_another_funds_names(paths) -> None:
    paths.paper.mkdir(parents=True, exist_ok=True)
    deploy("alpha", _spec("alpha"), ["RELIANCE"], root=paths.paper)
    broken = paths.paper / "broken"
    broken.mkdir()
    (broken / "fund.yaml").write_text("not: a: fund\n")
    assert deployed_universe(paths) == ("RELIANCE",)


def test_the_ingest_is_bounded() -> None:
    """One run is capped so a scheduler job cannot be handed a hundred-name
    batch that overruns its window; the rest belong to a second run."""
    too_many = [f"SYM{i}" for i in range(MAX_INGEST_SYMBOLS + 1)]
    with pytest.raises(IngestError, match="at most"):
        ingest_symbols(None, too_many)  # type: ignore[arg-type] - bound checked first


def test_symbols_are_normalised_and_deduplicated(tmp_path) -> None:
    db = tmp_path / "bq.db"
    migrate(db, create=True)
    seen: list[str] = []

    class _Fake:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def ingest(self, symbol: str):
            from bq.data.vendors.yahoo_fundamentals_vendor import IngestReport

            seen.append(symbol)
            return IngestReport(
                symbol=symbol, facts=False, snapshots=0, earnings=0, reason="skip"
            )

    import bq.aihf.ingest as ingest

    original = ingest.YahooFundamentalsIngest
    ingest.YahooFundamentalsIngest = _Fake  # type: ignore[assignment]
    try:
        summary = ingest_symbols(db, [" reliance ", "RELIANCE", "tcs"])
    finally:
        ingest.YahooFundamentalsIngest = original  # type: ignore[assignment]
    assert seen == ["RELIANCE", "TCS"]
    assert summary.symbols == ("RELIANCE", "TCS")
