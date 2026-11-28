"""Paper trading — a deployed fund, its ledger, and the tick that advances it.

    paper/<name>/fund.yaml        the DeployedFund: mandate snapshot + universe
    paper/<name>/ledger/          one SessionRecord per session, hash-chained
                                  (a minute fund's bars sit in a directory per day,
                                  with a checkpoint once the day closes)
    paper/<name>/ledger/superseded/   records replaced by a redo, off the chain
    paper/<name>/broker.json      the PaperBroker's own book
    paper/<name>/control.json     the kill switch
    paper/<name>/events.jsonl     halts, failures, resumes, gaps, checkpoints

`tick` advances one session; BharatQuant's scheduler calls it when one is due.
The unattended `run` loop and its `runner.pid` bookkeeping are **not** vendored:
this app owns scheduling (its job store), and a second event loop per fund would
duplicate the lease and retry semantics it already has. Backtests never import
anything from here: research reads the shared data cache and writes only to
research/.
"""

from bq.aihf.engine.paper.deployed import (
    deploy,
    DeployedFund,
    FUND_FILE,
    list_deployed,
    load_deployed,
    validate_fund_name,
)
from bq.aihf.engine.paper.ledger import Ledger, LedgerError
from bq.aihf.engine.paper.tick import (
    assessment_dates,
    following_session,
    next_session,
    NothingDue,
    redo,
    tick,
)

__all__ = [
    "DeployedFund",
    "FUND_FILE",
    "Ledger",
    "LedgerError",
    "NothingDue",
    "assessment_dates",
    "deploy",
    "following_session",
    "list_deployed",
    "load_deployed",
    "next_session",
    "redo",
    "tick",
    "validate_fund_name",
]
