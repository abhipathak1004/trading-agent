"""The engine's application surface — what `bq.aihf.service` calls.

Upstream this was `hedge_fund/run.py`: an argv CLI that parsed flags, built a
`FDClient`, printed a summary to stderr and JSON to stdout. That shape existed
because `aihf` was a separate process. In this port the engine is a library
inside the app, so the flags are gone and what remains is the ordered set of
operations the service actually needs. Keeping them here (rather than in the
service) means the service speaks in domain terms — deploy a fund, tick it —
and every path into the engine goes through one module.

Nothing here reads the environment, prints, implements a CLI, or constructs a
data or LLM client: both are injected, which is what lets the app supply its own
vendor router and model router.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from bq.aihf.engine.backtesting.fund import (
    FundBacktestResult,
    backtest_fund,
)
from bq.aihf.engine.brokers.costs import TradingCosts
from bq.aihf.engine.brokers.sim import SimBroker
from bq.aihf.engine.data.protocol import DataClient
from bq.aihf.engine.fund import Fund, FundSpec, load_spec
from bq.aihf.engine.llm import LLMClient, PromptCache
from bq.aihf.engine.paper.deployed import (
    DeployedFund,
    deploy,
    load_deployed,
    validate_fund_name,
)
from bq.aihf.engine.paper.ledger import Ledger
from bq.aihf.engine.paper.tick import NothingDue, next_session, redo, tick
from bq.aihf.engine.pipeline.session import FundHalted, FundState, SessionRecord, advance

__all__ = [
    "DeployedFund",
    "FundBacktestResult",
    "FundHalted",
    "FundState",
    "Ledger",
    "NothingDue",
    "SessionRecord",
    "build_fund",
    "create_fund",
    "halt_fund",
    "load_deployed",
    "load_spec",
    "next_session",
    "predict_session",
    "redo_last_session",
    "resume_fund",
    "run_backtest",
    "tick_fund",
    "validate_fund_name",
]


def build_fund(
    spec: FundSpec,
    *,
    llm: LLMClient | None = None,
    cache: PromptCache | None = None,
    blind: bool = False,
) -> Fund:
    """Staff a mandate. `llm` is required when the mandate names any persona."""
    return Fund(spec, blind=blind, llm=llm, cache=cache)


def run_backtest(
    spec: FundSpec,
    *,
    start: str,
    end: str,
    data_client: DataClient,
    universe: list[str] | None,
    llm: LLMClient | None = None,
    cache: PromptCache | None = None,
    on_valuation: Callable[[int, int, object], None] | None = None,
) -> FundBacktestResult:
    """Replay a mandate over history. Backtests are always `blind`: the prompt
    withholds the ticker and calendar dates so a result cannot lean on what the
    model remembers about a named company."""
    fund = build_fund(spec, llm=llm, cache=cache, blind=True)
    return backtest_fund(
        fund,
        start,
        end,
        data_client,
        universe,
        on_valuation=on_valuation,  # type: ignore[arg-type]
    )


def create_fund(
    name: str,
    spec: FundSpec,
    universe: list[str],
    *,
    root: Path | None = None,
) -> Path:
    """Deploy a paper fund: `fund.yaml`, an empty hash-chained ledger and a
    broker book seeded with the mandate's capital."""
    return deploy(name, spec, universe, root=root)


def tick_fund(
    directory: str | Path,
    *,
    data_client: DataClient,
    llm: LLMClient | None = None,
    cache: PromptCache | None = None,
) -> SessionRecord:
    """Advance a deployed fund by exactly one due session and record it.

    Raises `NothingDue` when no session can run yet and `FundHalted` when the
    kill switch is set — both are ordinary outcomes a caller displays, not
    failures.
    """
    return tick(
        directory,
        data_client,
        build_fund=lambda spec: build_fund(spec, llm=llm, cache=cache),
    )


def redo_last_session(
    directory: str | Path,
    *,
    data_client: DataClient,
    llm: LLMClient | None = None,
    cache: PromptCache | None = None,
) -> SessionRecord:
    """Run the latest recorded session again at the same marks, replacing it."""
    return redo(
        directory,
        data_client,
        build_fund=lambda spec: build_fund(spec, llm=llm, cache=cache),
    )


def predict_session(
    directory: str | Path,
    *,
    data_client: DataClient,
    llm: LLMClient | None = None,
    cache: PromptCache | None = None,
    spec: FundSpec | None = None,
    deployed: DeployedFund | None = None,
) -> SessionRecord:
    """Compute the next due session **without writing anything**.

    This is the approval gate's compute step. `advance` is already a pure
    function of (fund, state, session, broker, data_client), so running it
    against a broker rebuilt from the ledger in memory yields exactly the record
    the real tick would append — nothing on disk changes. `bq.aihf.service`
    stores that record and adopts it on approval.

    Raises `NothingDue` when nothing is due and `FundHalted` when the kill
    switch is set, checked *before* any compute.
    """
    directory = Path(directory)
    deployed = deployed or load_deployed(directory)
    spec = spec or deployed.spec
    ledger = Ledger(directory)
    state = ledger.replay(spec.capital)
    if state.halted is not None:
        raise FundHalted(f"{deployed.name} is halted: {state.halted}")
    due = next_session(data_client, spec, state.last_session)
    if due is None:
        raise NothingDue(f"{deployed.name}: no session is due yet")
    broker = SimBroker(cash=state.cash, costs=spec.costs, shares=dict(state.positions))
    fund = build_fund(spec, llm=llm, cache=cache)
    return advance(fund, state, due, broker, data_client, deployed.universe, None, None)


def halt_fund(directory: str | Path, reason: str) -> None:
    Ledger(directory).halt(reason)


def resume_fund(directory: str | Path) -> None:
    Ledger(directory).resume()


def costs_for(spec: FundSpec) -> TradingCosts:  # pragma: no cover - re-export aid
    """The mandate's cost model; kept so a caller need not import brokers.costs."""
    return spec.costs
