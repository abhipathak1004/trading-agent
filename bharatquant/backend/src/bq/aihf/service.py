"""The AI Hedge Fund service: mandates, backtests, and the paper desk.

Everything here is a projection of what the ported engine wrote, plus the two
places BharatQuant adds control: the mandate file the owner edits, and the
approval gate in front of a paper session.

The invariant this module exists to keep: **no paper session executes without an
explicit owner approval.** ADR-050 kept it by copying the fund, ticking the copy,
and adopting what it wrote. The engine runs in-process now (ADR-053), so the gate
is stronger *and* simpler: ``advance`` is a pure function of
(fund, state, session, broker, data), so a preview computes the exact record
against a broker rebuilt in memory and writes **nothing**. Approval appends that
record, and ``Ledger.append`` re-verifies its hash and that it chains to the
head — a preview file edited by hand cannot be admitted. No sandbox directories,
no directory digests, one LLM spend per session instead of two.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from bq.aihf.config import AiHfConfigError, AiHfPaths, installed_version, validate_name
from bq.aihf.engine import api as engine
from bq.aihf.engine.brokers.paper import PaperBroker
from bq.aihf.engine.data.protocol import DataClient
from bq.aihf.engine.fund.spec import FundSpec, load_spec
from bq.aihf.engine.llm import PromptCache
from bq.aihf.engine.paper.deployed import BROKER_FILE, load_deployed
from bq.aihf.engine.paper.ledger import Ledger
from bq.aihf.engine.paper.tick import NothingDue
from bq.aihf.engine.pipeline.clock import set_clock_factory
from bq.aihf.engine.pipeline.session import FundHalted, SessionRecord
from bq.aihf.ledger import (
    LedgerReadError,
    control_state,
    read_head,
    read_ledger,
    recent_events,
)
from bq.aihf.parsing import (
    AiHfParseError,
    BacktestSummary,
    LedgerView,
    SessionView,
    parse_backtest_detail,
    parse_backtest_summary,
    parse_session,
    trade_rows,
)
from bq.aihf.runner import read_json
from bq.aihf.runtime_env import build_runtime
from bq.model_router import ModelRouter
from bq.settings import SettingsStore

BACKTEST_TIMEOUT_SECONDS = 60 * 60 * 3
MAX_PREVIEW_AGE_SECONDS = 60 * 30
MAX_TICKERS = 25
MAX_DATE_SPAN_DAYS = 365 * 6
MAX_YAML_CHARS = 20_000


class AiHfServiceError(RuntimeError):
    """A request cannot be honoured (not configured, not found, conflicting)."""


class AiHfNotFound(AiHfServiceError):
    pass


class AiHfConflict(AiHfServiceError):
    pass


class AiHfInvalid(AiHfServiceError):
    pass


# ---------------------------------------------------------------------------
# request validation
# ---------------------------------------------------------------------------


def normalize_tickers(values: Sequence[str]) -> tuple[str, ...]:
    """Uppercase, deduplicate, bound — the engine's ``normalize_universe``, bounded."""
    cleaned: list[str] = []
    for value in values:
        ticker = value.strip().upper()
        if not ticker:
            continue
        if not all(char.isalnum() or char in ".-" for char in ticker):
            raise AiHfInvalid(f"invalid ticker {ticker!r}")
        if ticker not in cleaned:
            cleaned.append(ticker)
        if len(cleaned) > MAX_TICKERS:
            raise AiHfInvalid(f"at most {MAX_TICKERS} tickers per run")
    if not cleaned:
        raise AiHfInvalid("a run needs at least one ticker")
    return tuple(cleaned)


def validate_dates(start: str, end: str) -> tuple[str, str]:
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
    except ValueError as exc:
        raise AiHfInvalid("Dates must be YYYY-MM-DD.") from exc
    if first > last:
        raise AiHfInvalid("The start date must not be after the end date.")
    if last > date.today():
        raise AiHfInvalid("The end date must not be in the future.")
    if (last - first).days > MAX_DATE_SPAN_DAYS:
        raise AiHfInvalid(f"At most {MAX_DATE_SPAN_DAYS // 365} years per backtest.")
    return first.isoformat(), last.isoformat()


# ---------------------------------------------------------------------------
# status and models
# ---------------------------------------------------------------------------


def _models_list() -> list[dict[str, object]]:
    """Every analyst the engine can staff, read from the engine's own registry.

    One list, so a persona or quant model added to the vendored code appears here
    with no second registry to keep in step.
    """
    from bq.aihf.bridge.llm import PERSONA_AGENTS
    from bq.aihf.engine.signals import ALPHA_MODEL_REGISTRY

    models: list[dict[str, object]] = []
    for name in sorted(ALPHA_MODEL_REGISTRY):
        agent_id = PERSONA_AGENTS.get(name)
        models.append(
            {
                "display_name": name.replace("_", " ").title(),
                "model_name": name,
                "provider": "persona" if agent_id else "deterministic",
                "agent_id": agent_id,
                "runnable": True,
            }
        )
    return models


def status(paths: AiHfPaths, settings: SettingsStore) -> dict[str, object]:  # noqa: ARG001
    """Everything the header bar needs, with no key material."""
    runtime = build_runtime(settings)
    models = _models_list()
    has_llm = bool(runtime.describe()["has_llm_key"])
    return {
        # The engine is vendored, so it is always available. The field stays
        # because the UI contract parses it and two tests assert on it.
        "installed": True,
        "version": installed_version(),
        "active_provider": runtime.provider,
        "active_model": runtime.model,
        "bq_provider": settings.load().provider.value,
        "has_llm_key": has_llm,
        # There is no data key any more: the engine reads this app's own stores.
        "has_financial_datasets_key": True,
        "configured_vars": list(runtime.configured_vars),
        "missing_vars": list(runtime.missing),
        "models": models,
        "registry_error": None,
        "can_run": bool(has_llm and models),
    }


def models(paths: AiHfPaths, settings: SettingsStore) -> list[dict[str, object]]:  # noqa: ARG001
    return _models_list()


# ---------------------------------------------------------------------------
# mandates
# ---------------------------------------------------------------------------

EXAMPLE_MANDATE: dict[str, Any] = {
    "schema_version": 2,
    "name": "new-fund",
    "strategies": [
        {
            "name": "deep-value",
            "display_name": "Deep Value",
            "blend": {"mode": "long_only"},
            "weight": 0.6,
            "models": [
                {"name": "graham", "weight": 2.0},
                {"name": "buffett"},
                {"name": "munger"},
            ],
        },
        {
            "name": "earnings-drift",
            "display_name": "Earnings Drift",
            "blend": {"mode": "long_short"},
            "weight": 0.4,
            "models": [{"name": "pead"}],
        },
    ],
    "risk": {
        "max_position_pct": 0.25,
        "max_gross_exposure": 1.0,
        "halve_drawdown_pct": 0.1,
        "max_drawdown_pct": 0.2,
    },
    "capital": 1_000_000,
    "rebalance": "weekly",
    "benchmark": "NIFTY 50",
}


class MandateStore:
    """The owner's YAML mandates, validated by the engine's own ``FundSpec``."""

    def __init__(self, paths: AiHfPaths) -> None:
        self.paths = paths

    def list(self) -> list[dict[str, object]]:
        self._ensure_seeded()
        if not self.paths.mandates.is_dir():
            return []
        entries: list[dict[str, object]] = []
        for path in sorted(self.paths.mandates.glob("*.yaml")):
            try:
                validate_name(path.stem, what="mandate")
            except AiHfConfigError:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            entries.append(self._payload(path.stem, text, path))
        return entries

    def read(self, name: str) -> dict[str, object]:
        path = self.paths.mandate_path(name)
        if not path.is_file():
            raise AiHfNotFound(f"No mandate named {name!r}.")
        return self._payload(
            path.stem, path.read_text(encoding="utf-8", errors="replace"), path
        )

    def validate(self, text: str) -> dict[str, object]:
        """Validate in-process, where ``FundSpec`` lives, and summarise it."""
        if len(text) > MAX_YAML_CHARS:
            return {
                "ok": False,
                "error": f"The mandate YAML exceeds {MAX_YAML_CHARS} characters.",
            }
        try:
            spec = _load_spec_from_text(text)
        except (ValueError, AiHfConfigError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "summary": _mandate_summary(spec)}

    def write(self, name: str, text: str, *, create: bool) -> dict[str, object]:
        target = validate_name(name, what="mandate")
        path = self.paths.mandate_path(target)
        if create and path.exists():
            raise AiHfConflict(f"A mandate named {target!r} already exists.")
        if not create and not path.is_file():
            raise AiHfNotFound(f"No mandate named {target!r}.")
        summary = self.validate(text)
        if not summary.get("ok"):
            raise AiHfInvalid(str(summary.get("error") or "invalid mandate"))
        self.paths.mandates.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
        return self._payload(target, text, path)

    def delete(self, name: str) -> None:
        path = self.paths.mandate_path(name)
        if not path.is_file():
            raise AiHfNotFound(f"No mandate named {name!r}.")
        path.unlink()

    def template(self) -> str:
        return json.dumps(EXAMPLE_MANDATE, indent=2)

    def load(self, name: str) -> FundSpec:
        """The parsed spec of a stored mandate (raises ``AiHfNotFound``)."""
        path = self.paths.mandate_path(name)
        if not path.is_file():
            raise AiHfNotFound(f"No mandate named {name!r}.")
        try:
            return load_spec(path)
        except ValueError as exc:
            raise AiHfInvalid(str(exc)) from exc

    def _ensure_seeded(self) -> None:
        """Seed the mandates directory with the engine's example, exactly once.

        Only an *absent* directory is seeded, so an owner who deletes the example
        keeps it deleted.
        """
        if self.paths.mandates.is_dir():
            return
        self.paths.mandates.mkdir(parents=True, exist_ok=True)
        example = self.paths.mandates / "example.yaml"
        if not example.exists():
            from bq.aihf.engine.paths import EXAMPLE_MANDATE as UPSTREAM_EXAMPLE

            if UPSTREAM_EXAMPLE.is_file():
                example.write_text(
                    UPSTREAM_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
                )

    def _payload(self, name: str, text: str, path: Path) -> dict[str, object]:
        summary = self.validate(text)
        return {
            "name": name,
            "yaml": text,
            "valid": summary.get("ok", False),
            "error": summary.get("error"),
            "summary": summary.get("summary"),
            "updated_at": _mtime(path),
        }


def _load_spec_from_text(text: str) -> FundSpec:
    """Validate YAML text without leaving a file behind (``load_spec`` reads a path).

    ``load_spec`` prefixes every error with the path it read, which for in-memory
    validation is a temporary file name the owner never sees. The prefix is
    stripped so a validation error reads as the field problem it is.
    """
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=True) as handle:
        handle.write(text)
        handle.flush()
        try:
            return load_spec(handle.name)
        except ValueError as exc:
            message = str(exc)
            prefix = f"{handle.name}: "
            if message.startswith(prefix):
                message = message[len(prefix) :]
            raise ValueError(message) from exc


def _mandate_summary(spec: FundSpec) -> dict[str, object]:
    """The mandate's shape, as the UI's mandate list shows it."""
    return {
        "name": spec.name,
        "schema_version": 2,
        "capital": spec.capital,
        "rebalance": spec.rebalance,
        "benchmark": spec.benchmark,
        "is_intraday": spec.is_intraday,
        "has_universe_rule": spec.universe is not None,
        "risk": {
            "max_position_pct": spec.risk.max_position_pct,
            "max_gross_exposure": spec.risk.max_gross_exposure,
            "halve_drawdown_pct": spec.risk.halve_drawdown_pct,
            "max_drawdown_pct": spec.risk.max_drawdown_pct,
        },
        "strategies": [
            {
                "name": strategy.name,
                "title": strategy.title,
                "weight": strategy.weight,
                "mode": strategy.blend.mode,
                "models": [
                    {"name": model.name, "weight": model.weight}
                    for model in strategy.models
                ],
            }
            for strategy in spec.strategies
        ],
    }


# ---------------------------------------------------------------------------
# backtests (saved results)
# ---------------------------------------------------------------------------


class ResearchStore:
    """Saved backtest runs: ``research/<run-id>.json``."""

    def __init__(self, paths: AiHfPaths) -> None:
        self.paths = paths

    def list(self) -> list[dict[str, object]]:
        if not self.paths.research.is_dir():
            return []
        files = sorted(
            self.paths.research.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        summaries: list[dict[str, object]] = []
        for path in files:
            parsed = read_json(path)
            if parsed is None:
                continue
            try:
                summary = parse_backtest_summary(parsed, path=path.name)
            except AiHfParseError:
                continue
            summaries.append(summary_payload(summary, path))
        return summaries

    def resolve(self, run_id: str) -> Path:
        name = validate_name(run_id, what="backtest run")
        path = self.paths.research / f"{name}.json"
        if not path.is_file():
            raise AiHfNotFound(f"No saved backtest named {run_id!r}.")
        return path

    def detail(
        self, run_id: str, *, limit: int | None = None, offset: int = 0
    ) -> dict[str, object]:
        path = self.resolve(run_id)
        parsed = read_json(path)
        if parsed is None:
            raise AiHfNotFound(f"{run_id!r} is not readable.")
        try:
            detail = parse_backtest_detail(parsed, path=path.name)
        except AiHfParseError as exc:
            raise AiHfServiceError(
                f"{run_id!r} is not a backtest result: {exc}"
            ) from exc
        sessions = detail.sessions
        if limit is not None:
            start = max(0, len(sessions) - offset - limit)
            sessions = sessions[start : len(sessions) - offset]
        return {
            "summary": summary_payload(detail.summary, path),
            "dates": detail.dates,
            "nav": detail.nav,
            "benchmark_nav": detail.benchmark_nav,
            "trades": trade_rows(detail),
            "sessions": [session_payload(session) for session in sessions],
            "session_total": len(detail.sessions),
        }

    def save(self, result: Any, run_id: str) -> Path:
        """Persist an engine result under the run id the routes look up."""
        name = validate_name(run_id, what="backtest run")
        self.paths.research.mkdir(parents=True, exist_ok=True)
        path = self.paths.research / f"{name}.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return path


def summary_payload(summary: BacktestSummary, path: Path) -> dict[str, object]:
    metrics = summary.metrics
    return {
        "run_id": path.stem if path.suffix == ".json" else path.name,
        "fund": summary.fund,
        "universe": summary.universe,
        "start": summary.start,
        "end": summary.end,
        "benchmark": summary.benchmark,
        "rebalance": summary.rebalance,
        "capital": summary.capital,
        "metrics": {
            "total_return_pct": metrics.total_return_pct,
            "annualized_return_pct": metrics.annualized_return_pct,
            "sharpe_ratio": metrics.sharpe_ratio,
            "max_drawdown_pct": metrics.max_drawdown_pct,
            "benchmark_return_pct": metrics.benchmark_return_pct,
            "excess_return_pct": metrics.excess_return_pct,
            "n_cycles": metrics.n_cycles,
            "n_orders": metrics.n_orders,
            "traded_notional": metrics.traded_notional,
            "trading_cost": metrics.trading_cost,
        },
        "updated_at": _mtime(path),
    }


def session_payload(session: SessionView) -> dict[str, object]:
    return {
        "session": session.session,
        "nav": session.nav,
        "cash": session.cash,
        "positions": session.positions,
        "marks": session.marks,
        "benchmark": session.benchmark,
        "benchmark_close": session.benchmark_close,
        "rebalance": session.rebalance,
        "hash": session.hash,
        "prev_hash": session.prev_hash,
        "llm_model": session.llm_model,
        "executed": execution_payload(session),
        "decision": decision_payload(session),
    }


def execution_payload(session: SessionView) -> dict[str, object] | None:
    executed = session.executed
    if executed is None:
        return None
    return {
        "as_of": executed.as_of,
        "orders": [
            {
                "ticker": order.ticker,
                "side": order.side,
                "quantity": order.quantity,
                "price": order.price,
                "notional": round(order.notional, 2),
            }
            for order in executed.orders
        ],
        "fills": [
            {
                "ticker": fill.ticker,
                "side": fill.side,
                "quantity": fill.quantity,
                "price": fill.price,
            }
            for fill in executed.fills
        ],
        "clamps": [clamp_payload(clamp) for clamp in executed.clamps],
        "nav": executed.nav,
        "cash": executed.cash,
        "traded_notional": round(executed.traded_notional, 2),
    }


def decision_payload(session: SessionView) -> dict[str, object] | None:
    decision = session.decision
    if decision is None:
        return None
    return {
        "as_of": decision.as_of,
        "universe": decision.universe,
        "target_weights": decision.target_weights,
        "final_weights": decision.final_weights,
        "drawdown": decision.drawdown,
        "is_stopped_out": decision.is_stopped_out,
        "is_halved": decision.is_halved,
        "clamps": [clamp_payload(clamp) for clamp in decision.clamps],
        "strategies": [
            {
                "name": strategy.name,
                "slice": strategy.slice,
                "final_contribution": strategy.final_contribution,
                "flat_reason": strategy.flat_reason,
                "signals": [
                    {
                        "model_name": signal.model_name,
                        "ticker": signal.ticker,
                        "date": signal.date,
                        "value": signal.value,
                        "side": signal.side,
                        "reasoning": signal.reasoning,
                    }
                    for signal in strategy.signals
                ],
            }
            for strategy in decision.strategies
        ],
    }


def clamp_payload(clamp: Any) -> dict[str, object]:
    return {
        "limit": clamp.limit,
        "ticker": clamp.ticker,
        "sector": clamp.sector,
        "before": clamp.before,
        "after": clamp.after,
    }


# ---------------------------------------------------------------------------
# paper funds
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Preview:
    """A computed-but-unapproved session, held aside for the owner's decision."""

    preview_id: str
    fund: str
    head: str | None
    session_count: int
    record: SessionView
    raw: dict[str, Any]
    created_at: float


class PaperStore:
    """Deployed funds, the approval gate, and the kill switch."""

    def __init__(
        self,
        paths: AiHfPaths,
        settings: SettingsStore,
        *,
        data_client: DataClient | None = None,
        router: ModelRouter | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self._data_client = data_client
        self._router = router

    # ---- listing ---------------------------------------------------------

    def funds(self) -> list[dict[str, object]]:
        return [self._fund_payload(name) for name in self._fund_names()]

    def fund(self, name: str) -> dict[str, object]:
        return self._fund_payload(validate_name(name, what="paper fund"))

    def _fund_names(self) -> list[str]:
        if not self.paths.paper.is_dir():
            return []
        names: list[str] = []
        for entry in sorted(self.paths.paper.iterdir()):
            if (entry / "fund.yaml").is_file():
                try:
                    names.append(validate_name(entry.name, what="paper fund"))
                except AiHfConfigError:
                    continue
        return names

    def _fund_payload(self, name: str) -> dict[str, object]:
        directory = self.paths.fund_dir(name)
        if not directory.is_dir():
            raise AiHfNotFound(f"No paper fund named {name!r}.")
        try:
            deployed = load_deployed(directory)
        except ValueError as exc:
            raise AiHfServiceError(str(exc)) from exc
        meta = self._fund_meta(name)
        control = control_state(self.paths, name)
        nav: float | None = None
        last_session: str | None = None
        chain_verified = False
        problem: str | None = None
        count = 0
        try:
            view = read_ledger(self.paths, name)
            chain_verified = view.chain_verified
            problem = view.problem
            count = len(view.sessions)
            if view.sessions:
                nav = view.sessions[-1].nav
                last_session = view.sessions[-1].session
        except (LedgerReadError, AiHfConfigError) as exc:
            problem = str(exc)
        capital = deployed.spec.capital
        return_pct = nav / capital - 1 if nav is not None and capital else None
        drawdown_pct = None
        series = _nav_series(self.paths, name)
        if series and nav is not None:
            peak = max(series)
            if peak > 0:
                drawdown_pct = max(0.0, 1 - nav / peak)
        return {
            "name": name,
            "mandate": deployed.spec.name,
            "universe": list(deployed.universe),
            "capital": capital,
            "rebalance": deployed.spec.rebalance,
            "benchmark": deployed.spec.benchmark,
            "cadence_label": _cadence_label(deployed.spec.rebalance),
            "strategies": [
                {
                    "name": strategy.name,
                    "title": strategy.title,
                    "weight": strategy.weight,
                    "mode": strategy.blend.mode,
                    "models": [model.name for model in strategy.models],
                }
                for strategy in deployed.spec.strategies
            ],
            "nav": nav,
            "return_pct": return_pct,
            "drawdown_pct": drawdown_pct,
            "status": "HALTED" if control["halted"] else "ACTIVE",
            "halt_reason": control["reason"],
            "halted_since": control["since"],
            "last_session": last_session,
            "session_count": count,
            "chain_verified": chain_verified,
            "problem": problem,
            "created": deployed.created or meta.get("created"),
        }

    def _fund_meta(self, name: str) -> dict[str, Any]:
        parsed = read_json(self.paths.fund_meta_path(name))
        return parsed if isinstance(parsed, dict) else {}

    # ---- create / kill switch -------------------------------------------

    async def create(
        self, *, name: str, mandate: str, tickers: Sequence[str]
    ) -> dict[str, object]:
        fund = validate_name(name, what="paper fund")
        if self.paths.fund_dir(fund).exists():
            raise AiHfConflict(f"A paper fund named {fund!r} already exists.")
        spec = MandateStore(self.paths).load(mandate)
        universe = normalize_tickers(tickers)
        try:
            engine.create_fund(fund, spec, list(universe), root=self.paths.paper)
        except FileExistsError as exc:
            raise AiHfConflict(str(exc)) from exc
        return self.fund(fund)

    async def kill_switch(
        self, name: str, *, action: str, reason: str | None
    ) -> dict[str, object]:
        fund = validate_name(name, what="paper fund")
        directory = self.paths.fund_dir(fund)
        if not directory.is_dir():
            raise AiHfNotFound(f"No paper fund named {fund!r}.")
        if action == "halt":
            engine.halt_fund(directory, reason or "halted from BharatQuant")
        elif action == "resume":
            engine.resume_fund(directory)
        else:
            raise AiHfInvalid("action must be 'halt' or 'resume'")
        return self.fund(fund)

    # ---- the approval gate ----------------------------------------------

    async def preview(self, name: str) -> dict[str, object]:
        """Compute the next session without writing anything to the fund.

        ``advance`` is pure given (fund, state, session, broker, data), so the
        broker is rebuilt from the ledger in memory and the session runs against
        it. Nothing in the fund directory is touched: no ledger append, no broker
        rewrite. The record is stored for approval to adopt.
        """
        fund = validate_name(name, what="paper fund")
        directory = self.paths.fund_dir(fund)
        if not directory.is_dir():
            raise AiHfNotFound(f"No paper fund named {fund!r}.")
        control = control_state(self.paths, fund)
        if control["halted"]:
            raise AiHfConflict(
                f"{fund} is halted: {control['reason'] or 'no reason given'}"
            )

        deployed = load_deployed(directory)
        head, session_count = read_head(self.paths, fund)
        try:
            record = engine.predict_session(
                directory,
                data_client=self._data(),
                llm=None,
                cache=self._cache(),
                spec=deployed.spec,
                deployed=deployed,
            )
        except (NothingDue, FundHalted) as exc:
            raise AiHfConflict(str(exc)) from exc

        raw = record.model_dump()
        token = uuid.uuid4().hex[:16]
        preview = Preview(
            preview_id=f"{token}.{session_count}",
            fund=fund,
            head=head,
            session_count=session_count,
            record=parse_session(raw),
            raw=raw,
            created_at=time.time(),
        )
        self._write_preview(preview)
        return self._preview_payload(preview)

    async def execute(self, name: str, *, preview_id: str) -> dict[str, object]:
        """Adopt an approved preview into the live fund.

        The preview holds a record the engine computed and hashed. Approving it
        appends that exact record; ``Ledger.append`` re-verifies the hash and that
        ``prev_hash`` chains to the current head, so a preview edited by hand is
        refused. The broker's book is then written from the record's own positions
        and cash — the same book ``advance`` computed.
        """
        fund = validate_name(name, what="paper fund")
        directory = self.paths.fund_dir(fund)
        if not directory.is_dir():
            raise AiHfNotFound(f"No paper fund named {fund!r}.")

        # The kill switch is checked before anything else: a halted fund must
        # never be tellable "no preview" and then committable once one exists.
        control = control_state(self.paths, fund)
        if control["halted"]:
            raise AiHfConflict(
                f"{fund} is halted: {control['reason'] or 'no reason given'}"
            )

        preview = self._load_preview(fund)
        if preview.preview_id != preview_id:
            raise AiHfNotFound("That preview is no longer current; run it again.")
        if time.time() - preview.created_at > MAX_PREVIEW_AGE_SECONDS:
            self.clear_preview(fund)
            raise AiHfConflict("This preview is stale; run the session again.")
        head, _ = read_head(self.paths, fund)
        if head != preview.head:
            self.clear_preview(fund)
            raise AiHfConflict(
                "The ledger advanced since this preview; run the session again."
            )

        deployed = load_deployed(directory)
        record = SessionRecord.model_validate(preview.raw)
        ledger = Ledger(directory)
        try:
            ledger.append(record)
        except Exception as exc:  # noqa: BLE001 - a forged or stale record is a 409
            self.clear_preview(fund)
            raise AiHfConflict(f"The preview could not be adopted: {exc}") from exc
        PaperBroker.restore(
            directory / BROKER_FILE, record.cash, record.positions, deployed.spec.costs
        )
        ledger.log_event("approved", session=record.session)
        self.clear_preview(fund)
        return {
            "fund": self.fund(fund),
            "adopted": session_payload(parse_session(record.model_dump())),
        }

    def ledger(
        self,
        name: str,
        *,
        limit: int | None = 200,
        offset: int = 0,
        verify: bool = True,
    ) -> dict[str, object]:
        fund = validate_name(name, what="paper fund")
        directory = self.paths.fund_dir(fund)
        if not directory.is_dir():
            raise AiHfNotFound(f"No paper fund named {fund!r}.")
        view: LedgerView = read_ledger(self.paths, fund, limit=limit, offset=offset)
        verified = view.chain_verified
        verify_error: str | None = None
        if verify:
            # In-process now: the engine's own replay recomputes every hash, so
            # this is the authentic check rather than a subprocess probe.
            try:
                deployed = load_deployed(directory)
                Ledger(directory).replay(deployed.spec.capital, full=True)
                verified = True
            except Exception as exc:  # noqa: BLE001 - a broken chain is reported, not raised
                verified = False
                verify_error = _bounded(str(exc))
        return {
            "fund": fund,
            "sessions": [session_payload(session) for session in view.sessions],
            "session_total": len(view.sessions),
            "head": view.head,
            "chain_verified": verified,
            "problem": view.problem,
            "verify_error": verify_error,
            "events": recent_events(self.paths, fund),
            "control": control_state(self.paths, fund),
        }

    # ---- internals -------------------------------------------------------

    def _data(self) -> DataClient:
        if self._data_client is not None:
            return self._data_client
        from bq.aihf.wiring import build_data_client

        return build_data_client(self.settings)

    def _cache(self) -> PromptCache:
        return PromptCache()

    def _preview_path(self, fund: str) -> Path:
        return self.paths.previews / f"{fund}.json"

    def _write_preview(self, preview: Preview) -> None:
        path = self._preview_path(preview.fund)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "preview_id": preview.preview_id,
            "fund": preview.fund,
            "head": preview.head,
            "session_count": preview.session_count,
            "created_at": preview.created_at,
            "record": preview.raw,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _load_preview(self, fund: str) -> Preview:
        parsed = read_json(self._preview_path(fund))
        if not isinstance(parsed, dict):
            raise AiHfNotFound("There is no preview to approve; run the session first.")
        record = parsed.get("record")
        if not isinstance(record, dict):
            raise AiHfServiceError("The stored preview has no session record.")
        created = parsed.get("created_at")
        return Preview(
            preview_id=str(parsed.get("preview_id") or ""),
            fund=fund,
            head=parsed.get("head") if isinstance(parsed.get("head"), str) else None,
            session_count=int(parsed.get("session_count") or 0),
            record=parse_session(record),
            raw=dict(record),
            created_at=float(created) if isinstance(created, (int, float)) else 0.0,
        )

    def clear_preview(self, fund: str) -> None:
        """Drop a pending preview. There is no sandbox to remove any more."""
        name = validate_name(fund, what="paper fund")
        self._preview_path(name).unlink(missing_ok=True)

    def _preview_payload(self, preview: Preview) -> dict[str, object]:
        record = preview.record
        executed = record.executed
        decision = record.decision
        clamps = list(executed.clamps if executed else ()) + list(
            decision.clamps if decision else ()
        )
        payload = execution_payload(record)
        return {
            "preview_id": preview.preview_id,
            "fund": preview.fund,
            "session": record.session,
            "head": preview.head,
            "nav": record.nav,
            "cash": record.cash,
            "rebalance": record.rebalance,
            "benchmark": record.benchmark,
            "benchmark_close": record.benchmark_close,
            "orders": [] if payload is None else payload["orders"],
            "fills": [] if payload is None else payload["fills"],
            "risk_checks": [
                {
                    **clamp_payload(clamp),
                    # a clamp only ever shrinks the book (risk/limits.py).
                    "passed": abs(clamp.after) <= abs(clamp.before) + 1e-9,
                }
                for clamp in clamps
            ],
            "reasoning": _reasoning(record),
            "positions": record.positions,
            "marks": record.marks,
            "stopped_out": bool(decision and decision.is_stopped_out),
            "halved": bool(decision and decision.is_halved),
        }


def _bounded(text: str, limit: int = 400) -> str:
    return " ".join(text.split())[:limit]


def _nav_series(paths: AiHfPaths, fund: str) -> list[float]:
    try:
        return [session.nav for session in read_ledger(paths, fund).sessions]
    except (LedgerReadError, AiHfConfigError):
        return []


def _cadence_label(rebalance: str) -> str:
    return {
        "minute": "every bar",
        "daily": "daily",
        "weekly": "weekly",
        "monthly": "monthly",
    }.get(rebalance, rebalance)


def _reasoning(record: SessionView) -> list[dict[str, object]]:
    decision = record.decision
    if decision is None:
        return []
    return [
        {
            "strategy": strategy.name,
            "model_name": signal.model_name,
            "ticker": signal.ticker,
            "value": signal.value,
            "side": signal.side,
            "reasoning": signal.reasoning,
        }
        for strategy in decision.strategies
        for signal in strategy.signals
    ]


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
    except OSError:
        return None


def install_market_clock() -> None:
    """Point the engine at this app's market clock (NSE/IST).

    Called once at start-up. Without it, `clock_for` returns upstream's New York
    clock, and a fund would be a day off in a way nothing else surfaces.
    """
    from bq.aihf.bridge.clock import nse_clock_factory

    set_clock_factory(nse_clock_factory())


__all__ = [
    "AiHfConflict",
    "AiHfInvalid",
    "AiHfNotFound",
    "AiHfServiceError",
    "MandateStore",
    "PaperStore",
    "ResearchStore",
    "clamp_payload",
    "decision_payload",
    "execution_payload",
    "install_market_clock",
    "models",
    "normalize_tickers",
    "session_payload",
    "status",
    "summary_payload",
    "validate_dates",
]
