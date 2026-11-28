"""The AI Hedge Fund service: mandates, backtests, and the paper desk.

Everything here is a projection of what ``aihf`` wrote, plus the two places
BharatQuant adds control: the mandate file the owner edits, and the approval
gate in front of a paper session.

The invariant this module exists to keep: **no paper session executes without
an explicit owner approval.** ``aihf paper tick`` decides *and* executes in one
call, so a preview runs that call against a *copy* of the fund, and approval
adopts the copy's aihf-written state back over the live fund. Because every
byte adopted was written by aihf itself (ledger record, broker book,
events), aihf's own hash-chain check re-verifies it on the next read — we
never re-serialise or hand-build a record.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from bq.aihf.config import AiHfConfigError, AiHfPaths, installed_version, validate_name
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
    parse_session_document,
    trade_rows,
)
from bq.aihf.registry import (
    PROBE_DIR,
    AiHfModel,
    AiHfRegistryError,
    registry,
    run_probe,
)
from bq.aihf.runner import (
    AiHfResult,
    copy_tree,
    directory_digest,
    fund_names,
    read_json,
    remove_tree,
    run_aihf,
)
from bq.aihf.runtime_env import AIHF_ENV_VARS, DATA_KEY_ENV, build_runtime
from bq.settings import SettingsStore

VALIDATE_MANDATE_PROBE = PROBE_DIR / "validate_mandate.py"
VERIFY_LEDGER_PROBE = PROBE_DIR / "verify_ledger.py"
FUNDS_PROBE = PROBE_DIR / "funds.py"

BACKTEST_TIMEOUT_SECONDS = 60 * 60 * 3
TICK_TIMEOUT_SECONDS = 60 * 30
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
    """Uppercase, deduplicate, bound — aihf's ``normalize_universe``, bounded."""
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


def model_descriptor(model: AiHfModel, configured: set[str]) -> dict[str, object]:
    var = AIHF_ENV_VARS.get(model.provider)
    return {
        "display_name": model.display_name,
        "model_name": model.model_name,
        "provider": model.provider,
        "env_var": var,
        "runnable": bool(var and var in configured),
    }


def status(paths: AiHfPaths, settings: SettingsStore) -> dict[str, object]:
    """Everything the header bar needs, with no key material."""
    runtime = build_runtime(settings, environ={}, paths=paths)
    configured = set(runtime.configured_vars)
    try:
        models = registry(paths)
        registry_error: str | None = None
    except AiHfRegistryError as exc:
        models = ()
        registry_error = str(exc)
    return {
        "installed": paths.available,
        "version": installed_version(paths),
        "active_provider": runtime.provider,
        "active_model": runtime.model,
        "bq_provider": settings.load().provider.value,
        "has_llm_key": runtime.describe()["has_llm_key"],
        "has_financial_datasets_key": DATA_KEY_ENV in configured,
        "configured_vars": list(runtime.configured_vars),
        "missing_vars": list(runtime.missing),
        "models": [model_descriptor(model, configured) for model in models],
        "registry_error": registry_error,
        "can_run": bool(
            paths.available
            and runtime.describe()["has_llm_key"]
            and DATA_KEY_ENV in configured
            and models
        ),
    }


def models(paths: AiHfPaths, settings: SettingsStore) -> list[dict[str, object]]:
    runtime = build_runtime(settings, environ={}, paths=paths)
    configured = set(runtime.configured_vars)
    return [model_descriptor(model, configured) for model in registry(paths)]


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
        }
    ],
    "risk": {
        "max_position_pct": 0.25,
        "max_gross_exposure": 1.0,
        "halve_drawdown_pct": 0.1,
        "max_drawdown_pct": 0.2,
    },
    "capital": 100000,
    "rebalance": "weekly",
    "benchmark": "SPY",
}


class MandateStore:
    """The owner's YAML mandates, validated by aihf's own ``FundSpec``."""

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
        """Validate and summarise in the sidecar, where ``FundSpec`` lives."""
        if len(text) > MAX_YAML_CHARS:
            return {
                "ok": False,
                "error": f"The mandate YAML exceeds {MAX_YAML_CHARS} characters.",
            }
        try:
            payload = run_probe(
                self.paths, VALIDATE_MANDATE_PROBE, payload=text.encode("utf-8")
            )
        except AiHfRegistryError as exc:
            raise AiHfServiceError(str(exc)) from exc
        if not payload.get("ok"):
            return {
                "ok": False,
                "error": str(payload.get("error") or "invalid mandate"),
            }
        return {"ok": True, "summary": payload.get("summary")}

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

    def _ensure_seeded(self) -> None:
        """Seed the mandates directory with aihf's example, exactly once.

        Mirrors ``hedge_fund.paths.ensure_mandates_dir``: the example ships
        inside the package and is copied out, never read in place. Only an
        *absent* directory is seeded, so an owner who deletes the example
        keeps it deleted.

        The directory must not be created before calling: aihf's own guard is
        ``if not MANDATES_DIR.exists()``.
        """
        from bq.aihf.runner import (
            AiHfExecutionError,
            run_aihf_sync,
        )  # local: seeded once

        if self.paths.mandates.is_dir():
            return
        try:
            # Every aihf entry point seeds before it parses arguments.
            run_aihf_sync(self.paths, ["paper", "list"])
        except (AiHfExecutionError, OSError):
            return
        self.paths.mandates.mkdir(parents=True, exist_ok=True)

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


# ---------------------------------------------------------------------------
# backtests (saved results)
# ---------------------------------------------------------------------------


class ResearchStore:
    """Saved backtest runs: ``research/<fund>-<start>-<end>-<stamp>.json``."""

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
    token: str
    record: SessionView
    created_at: float
    digest_before: str


class PaperStore:
    """Deployed funds, the approval gate, and the kill switch."""

    def __init__(self, paths: AiHfPaths, settings: SettingsStore) -> None:
        self.paths = paths
        self.settings = settings

    # ---- listing ---------------------------------------------------------

    def funds(self) -> list[dict[str, object]]:
        """Every deployed fund, with its mandate metadata read by aihf itself."""
        self._funds_cache = self._read_fund_metadata()
        return [self._fund_payload(name) for name in fund_names(self.paths)]

    def fund(self, name: str) -> dict[str, object]:
        self._funds_cache = self._read_fund_metadata()
        return self._fund_payload(validate_name(name, what="paper fund"))

    def _read_fund_metadata(self) -> dict[str, dict[str, Any]]:
        """``name -> metadata``. ``fund.yaml`` is YAML, so aihf parses it."""
        try:
            payload = run_probe(
                self.paths, FUNDS_PROBE, payload=str(self.paths.paper).encode("utf-8")
            )
        except AiHfRegistryError as exc:
            raise AiHfServiceError(str(exc)) from exc
        if not payload.get("ok"):
            raise AiHfServiceError(
                str(payload.get("error") or "cannot read the paper funds")
            )
        funds = payload.get("funds")
        if not isinstance(funds, list):
            return {}
        return {
            str(entry.get("name")): entry
            for entry in funds
            if isinstance(entry, dict) and entry.get("name")
        }

    def _fund_payload(self, name: str) -> dict[str, object]:
        if not self.paths.fund_dir(name).is_dir():
            raise AiHfNotFound(f"No paper fund named {name!r}.")
        meta: dict[str, Any] = getattr(self, "_funds_cache", {}).get(
            name, {"error": "fund metadata unavailable"}
        )
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
        capital = meta.get("capital")
        return_pct = None
        if (
            isinstance(nav, (int, float))
            and isinstance(capital, (int, float))
            and capital
        ):
            return_pct = nav / float(capital) - 1
        drawdown_pct = None
        series = _nav_series(self.paths, name)
        if series and nav is not None:
            peak = max(series)
            if peak > 0:
                drawdown_pct = max(0.0, 1 - nav / peak)
        return {
            "name": name,
            "mandate": meta.get("mandate"),
            "universe": meta.get("universe")
            if isinstance(meta.get("universe"), list)
            else [],
            "capital": capital,
            "rebalance": meta.get("rebalance"),
            "benchmark": meta.get("benchmark"),
            "cadence_label": meta.get("cadence_label"),
            "strategies": meta.get("strategies", []),
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
            "created": meta.get("created"),
        }

    # ---- create / kill switch -------------------------------------------

    async def create(
        self, *, name: str, mandate: str, tickers: Sequence[str]
    ) -> dict[str, object]:
        # aihf seeds its example stash on first use, so seed before resolving.
        MandateStore(self.paths).list()
        fund = validate_name(name, what="paper fund")
        target = validate_name(mandate, what="mandate")
        mandate_path = self.paths.mandate_path(target)
        if not mandate_path.is_file():
            raise AiHfNotFound(f"No mandate named {target!r}.")
        if self.paths.fund_dir(fund).exists():
            raise AiHfConflict(f"A paper fund named {fund!r} already exists.")
        universe = normalize_tickers(tickers)
        result = await self._run(
            [
                "paper",
                "create",
                fund,
                "--mandate",
                str(mandate_path),
                "--universe",
                ",".join(universe),
            ]
        )
        if not result.ok:
            raise AiHfServiceError(result.failure_detail())
        return self.fund(fund)

    async def kill_switch(
        self, name: str, *, action: str, reason: str | None
    ) -> dict[str, object]:
        fund = validate_name(name, what="paper fund")
        if not self.paths.fund_dir(fund).is_dir():
            raise AiHfNotFound(f"No paper fund named {fund!r}.")
        if action == "halt":
            argv = [
                "paper",
                "halt",
                fund,
                "--reason",
                reason or "halted from BharatQuant",
            ]
        elif action == "resume":
            argv = ["paper", "resume", fund]
        else:
            raise AiHfInvalid("action must be 'halt' or 'resume'")
        result = await self._run(argv)
        if not result.ok:
            raise AiHfServiceError(result.failure_detail())
        return self.fund(fund)

    # ---- the approval gate ----------------------------------------------

    async def preview(
        self, name: str, *, model: str | None = None
    ) -> dict[str, object]:
        """Compute the next session against a copy of the fund; mutate nothing live."""
        fund = validate_name(name, what="paper fund")
        live = self.paths.fund_dir(fund)
        if not live.is_dir():
            raise AiHfNotFound(f"No paper fund named {fund!r}.")
        control = control_state(self.paths, fund)
        if control["halted"]:
            raise AiHfConflict(
                f"{fund} is halted: {control['reason'] or 'no reason given'}"
            )

        self.clear_preview(fund)
        head, session_count = read_head(self.paths, fund)
        token = uuid.uuid4().hex[:16]
        sandbox_home = self.paths.sandbox_home(token)
        sandbox_fund = sandbox_home / ".hedge-fund" / "paper" / fund
        digest_before = directory_digest(live)
        try:
            copy_tree(live, sandbox_fund)
            result = await self._run(
                ["paper", "tick", fund], model=model, home=sandbox_home
            )
            if not result.ok:
                raise AiHfServiceError(result.failure_detail())
            record = parse_session_document(result.stdout)
        except AiHfParseError as exc:
            remove_tree(sandbox_home)
            raise AiHfServiceError(str(exc)) from exc
        except BaseException:
            remove_tree(sandbox_home)
            raise

        if directory_digest(live) != digest_before:
            remove_tree(sandbox_home)
            raise AiHfServiceError(
                "The preview modified the live fund; refusing to continue."
            )

        preview = Preview(
            preview_id=f"{token}.{session_count}",
            fund=fund,
            head=head,
            session_count=session_count,
            token=token,
            record=record,
            created_at=time.time(),
            digest_before=digest_before,
        )
        self._write_preview(preview)
        return self._preview_payload(preview)

    async def execute(self, name: str, *, preview_id: str) -> dict[str, object]:
        """Adopt an approved preview into the live fund.

        The sandbox holds only aihf-written files (its ledger record, broker
        book and events), so adopting it cannot forge a record: aihf's own
        ``Ledger.replay`` re-verifies every hash on the next read.
        """
        fund = validate_name(name, what="paper fund")
        live = self.paths.fund_dir(fund)
        if not live.is_dir():
            raise AiHfNotFound(f"No paper fund named {fund!r}.")

        # The kill switch is checked before anything else: a halted fund must
        # never be told "no preview" and then be committable once one exists.
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
        if directory_digest(live) != preview.digest_before:
            self.clear_preview(fund)
            raise AiHfConflict(
                "The fund changed since this preview; run the session again."
            )

        sandbox_fund = (
            self.paths.sandbox_home(preview.token) / ".hedge-fund" / "paper" / fund
        )
        if not sandbox_fund.is_dir():
            self.clear_preview(fund)
            raise AiHfConflict(
                "The preview is no longer on disk; run the session again."
            )
        _adopt(sandbox_fund, live)
        self.clear_preview(fund)
        return {
            "fund": self.fund(fund),
            "adopted": session_payload(preview.record),
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
        if not self.paths.fund_dir(fund).is_dir():
            raise AiHfNotFound(f"No paper fund named {fund!r}.")
        view: LedgerView = read_ledger(self.paths, fund, limit=limit, offset=offset)
        verified = view.chain_verified
        verify_error: str | None = None
        if verify:
            try:
                payload = run_probe(
                    self.paths,
                    VERIFY_LEDGER_PROBE,
                    payload=str(self.paths.fund_dir(fund)).encode("utf-8"),
                )
                if payload.get("ok"):
                    verified = bool(payload.get("verified"))
                else:
                    verified = False
                    verify_error = str(payload.get("error"))
            except AiHfRegistryError as exc:
                verify_error = str(exc)
                verified = False
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
            "token": preview.token,
            "created_at": preview.created_at,
            "digest_before": preview.digest_before,
            "record": _raw_record(preview.record),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _load_preview(self, fund: str) -> Preview:
        parsed = read_json(self._preview_path(fund))
        if not isinstance(parsed, dict):
            raise AiHfNotFound("There is no preview to approve; run the session first.")
        record = parsed.get("record")
        try:
            session = parse_session(record)
        except AiHfParseError as exc:
            raise AiHfServiceError(str(exc)) from exc
        created = parsed.get("created_at")
        return Preview(
            preview_id=str(parsed.get("preview_id") or ""),
            fund=fund,
            head=parsed.get("head") if isinstance(parsed.get("head"), str) else None,
            session_count=int(parsed.get("session_count") or 0),
            token=str(parsed.get("token") or ""),
            record=session,
            created_at=float(created) if isinstance(created, (int, float)) else 0.0,
            digest_before=str(parsed.get("digest_before") or ""),
        )

    def clear_preview(self, fund: str) -> None:
        """Drop a pending preview and its sandbox. Never touches the live fund."""
        name = validate_name(fund, what="paper fund")
        path = self._preview_path(name)
        token: str | None = None
        parsed = read_json(path)
        if isinstance(parsed, dict) and isinstance(parsed.get("token"), str):
            token = parsed.get("token")
        path.unlink(missing_ok=True)
        if token:
            try:
                remove_tree(self.paths.sandbox_home(token))
            except AiHfConfigError:
                pass

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

    async def _run(
        self,
        argv: Sequence[str],
        *,
        model: str | None = None,
        home: Path | None = None,
    ) -> AiHfResult:
        runtime = build_runtime(
            self.settings, model_override=model, environ={}, paths=self.paths
        )
        env = dict(runtime.env)
        if home is not None:
            home.mkdir(parents=True, exist_ok=True)
            env["HOME"] = str(home)
        return await run_aihf(self.paths, argv, env, timeout=TICK_TIMEOUT_SECONDS)


def _adopt(sandbox_fund: Path, live: Path) -> None:
    """Copy aihf-written state from an approved sandbox over the live fund.

    Only the files aihf owns are adopted: the ledger (and its checkpoints),
    the broker's book, and the event log. ``fund.yaml`` and the kill switch
    are deliberately left alone — the mandate snapshot and a halt that
    happened during the preview must survive an approval.
    """
    ledger_dir = sandbox_fund / "ledger"
    if ledger_dir.is_dir():
        shutil.copytree(ledger_dir, live / "ledger", dirs_exist_ok=True)
    for name in ("broker.json", "events.jsonl"):
        source = sandbox_fund / name
        if source.is_file():
            shutil.copy2(source, live / name)


def _raw_record(session: SessionView) -> dict[str, Any]:
    """Re-read the record from disk so the stored preview is aihf's own JSON.

    ``parse_session`` bounds and normalises for the wire; the preview needs the
    untouched record for the adopt check to be meaningful.
    """
    return dict(session.raw) if session.raw else {}


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


def _nav_series(paths: AiHfPaths, fund: str) -> list[float]:
    try:
        return [session.nav for session in read_ledger(paths, fund).sessions]
    except (LedgerReadError, AiHfConfigError):
        return []


def deployed_meta(deployed: object) -> dict[str, Any]:
    if not isinstance(deployed, dict):
        return {}
    spec = deployed.get("spec")
    spec_map = spec if isinstance(spec, dict) else {}
    universe = deployed.get("universe")
    return {
        "mandate": spec_map.get("name"),
        "capital": spec_map.get("capital"),
        "rebalance": spec_map.get("rebalance"),
        "benchmark": spec_map.get("benchmark"),
        "universe": universe if isinstance(universe, list) else [],
        "created": deployed.get("created"),
    }


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
    except OSError:
        return None
