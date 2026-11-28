"""A deployed fund: a mandate pointed at a universe, with a home on disk.

A mandate is the desk and names no tickers. Deploying it fixes the universe
and gives it a directory that the ledger, the broker's book and the kill
switch live in. The mandate is snapshotted into fund.yaml: a paper fund's
rules must not drift under it between ticks, and `execute_decision` refuses
to execute a decision made under a different spec.
"""

from __future__ import annotations

import re
from datetime import date as _date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from bq.aihf.engine import paths
from bq.aihf.engine.brokers.paper import PaperBroker
from bq.aihf.engine.fund import FundSpec, normalize_universe
from bq.aihf.engine.paper.ledger import Ledger

FUND_FILE = "fund.yaml"
BROKER_FILE = "broker.json"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class DeployedFund(BaseModel):
    """What fund.yaml holds."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    name: str
    spec: FundSpec                      # snapshot of the mandate at deploy time
    universe: list[str]
    broker: Literal["paper"] = "paper"
    created: str                        # YYYY-MM-DD

    @field_validator("spec")
    @classmethod
    def _a_universe_rule_is_not_deployable_yet(cls, spec: FundSpec) -> FundSpec:
        # Refused here, at the door, so deploy, tick, redo, and the runner
        # all stop before a ledger or a broker file is touched.
        if spec.universe is not None:
            raise ValueError(
                f"{spec.name}: a mandate with a universe rule cannot run as a paper fund yet; backtest it instead"
            )
        return spec


def validate_fund_name(name: str) -> str:
    """A fund name is a directory name: no separators, nothing hidden."""
    if not _NAME.fullmatch(name) or name in (".", ".."):
        raise ValueError(
            f"invalid fund name {name!r}: use letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return name


def deploy(
    name: str, spec: FundSpec, universe: list[str], *, root: Path | None = None,
) -> Path:
    """Create paper/<name>/ with an empty ledger and a book seeded with the
    mandate's capital. Returns the fund's directory. Refuses to overwrite."""
    validate_fund_name(name)
    universe = normalize_universe(universe)
    directory = Path(root) if root is not None else paths.paper_dir()
    directory = directory / name
    if directory.exists():
        raise FileExistsError(f"paper fund {name!r} already exists at {directory}")
    deployed = DeployedFund(
        name=name, spec=spec, universe=universe, created=_date.today().isoformat(),
    )
    directory.mkdir(parents=True)
    (directory / FUND_FILE).write_text(
        yaml.safe_dump(deployed.model_dump(), sort_keys=False)
    )
    PaperBroker.create(directory / BROKER_FILE, spec.capital, spec.costs)
    Ledger(directory).init()
    return directory


def load_deployed(directory: str | Path) -> DeployedFund:
    """Read paper/<name>/fund.yaml, raising ValueError with the path on any problem."""
    path = Path(directory) / FUND_FILE
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: cannot read the deployed fund: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the deployed fund must be a YAML mapping")
    try:
        return DeployedFund.model_validate(data)
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"{path}: {errors}") from exc


def list_deployed(root: Path | None = None) -> list[Path]:
    """Every fund directory under *root* (default: the injected paper dir)."""
    directory = Path(root) if root is not None else paths.paper_dir()
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if (p / FUND_FILE).is_file())
