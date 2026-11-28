# Vendored engine — provenance and patch log

`bq/aihf/engine/` is **vendored third-party source**: the `hedge_fund` package
from [`aihf` 2.7.2](https://pypi.org/project/aihf/2.7.2/) (upstream
[`virattt/ai-hedge-fund`](https://github.com/virattt/ai-hedge-fund), v2 branch),
licensed MIT — see `LICENSE` (Copyright (c) 2024 Virat Singh).

It is kept as a faithful copy rather than restyled as our own source, which is
why `pyproject.toml` excludes it from ruff and relaxes strict mypy for it. Every
deviation from upstream is listed below with the reason; the goal is that a
future upgrade is a re-diff, not an archaeology exercise.

Supersedes ADR-050 (the sidecar subprocess). Decision record: ADR-052.

## Why vendored at all

Upstream pins `pandas<3`/`numpy<2`; this project is locked to
`pandas>=3.1`/`numpy>=2.5`. Reading the source showed the pin guards almost
nothing: pandas appears in one file, in one function that nothing calls, and
numpy in four places, each a scalar statistic. Porting those ~80 lines removes
the conflict, so the engine can share the app's environment and its data and
model layers instead of running beside them.

## What was removed

| Upstream path | Why |
|---|---|
| `tui/` (all) | Terminal UI. The app's own SPA is the interface. This is also what made `run.py` drag `textual`/`rich` into the headless path. |
| `run.py` | The argv CLI (`aihf backtest`, `aihf paper …`). Replaced by `bq/aihf/engine/api.py`, which the service calls directly, so argparse/rich/dotenv/textual are all gone. |
| `data/client.py` | `FDClient` — the Financial Datasets API client (and its x402 wallet). That is the key this port removes. `DataClientError` moved to `data/protocol.py`. |
| `data/wallet.py` | x402 per-call payments (`eth_account`). Not used. |
| `llm/client.py` | Every provider transport (langchain Anthropic/OpenAI/DeepSeek/Google/xAI/Kimi, plus the TypeSafe Jev adapter) and the `make_llm` factory. Replaced by `llm/protocol.py` (protocol + errors + `extract_json`) and the app's `bq/aihf/bridge/llm.py`. |
| `llm/contract.py` | The Jev request contract. Nothing else imported it. |
| `paper/runner.py` | The unattended per-fund event loop and its pid file. BharatQuant's scheduler owns scheduling; a second loop would duplicate the job store's lease/retry semantics. |
| `backtesting/research.py` | The CLI-era saved-result store (`research/<fund>-<start>-<end>-<stamp>.json` helpers). The app's `ResearchStore` owns this. |
| `* 2.py`, `* 2.yaml`, `* 2.json` | Duplicate copies that shipped inside the wheel. |
| `pyproject` probe mypy override (`bq.aihf.probe.*`) | The probe scripts it guarded are deleted with the sidecar. |

## What was changed, file by file

| File | Change | Reason |
|---|---|---|
| **all `*.py`** | `from hedge_fund…` → `from bq.aihf.engine…` | Vendored package path. |
| `signals/base.py` | Dropped `import pandas as pd` (L9) and `_compute_rsi` (L97–107); `np.isnan/isinf/tanh` → `math.*`. | `_compute_rsi` was never called by anything outside `tui/`. This is the only pandas use in the package. |
| `risk/exposures.py` | `np.cov/np.var/np.array` → `_ols_slope` + `statistics.stdev`. | One bivariate OLS slope; no linalg needed. |
| `scorecard/analysts.py` | `np.mean/std/array` → `statistics`; `_average_ranks`'s `np.unique(return_inverse, return_counts)` → `Counter` + running position. | Tie-averaged ranks, same definition. |
| `backtesting/fund.py` | `np.array` returns → list comprehension; `np.sqrt` → `math.sqrt`; `np.std/mean` → `statistics`. | Sharpe from per-session returns. |
| `llm/protocol.py` | **New** (extracted from upstream `llm/client.py`): `LLMClient`, `LLMCallError`, `LLMParseError`, `extract_json`, `DEFAULT_MODEL`. | The provider-agnostic half is still needed; only the transports are gone. |
| `llm/__init__.py` | Re-exports only the surviving symbols. | Deleted modules. |
| `signals/llm_agent.py` | `llm` is now a **required** argument (no `make_llm()` fallback); added `model_id` property. | No process-wide factory exists; the app injects its router-backed client. |
| `fund/spec.py` | `Fund.__init__` takes `llm=` and `cache=` and injects them into every `LLMAgent`; raises if a mandate names a persona and no client was supplied. | Personas are named by string in the mandate, so the client must come from the caller. |
| `pipeline/session.py` | `_llm_model` reports the injected client's `model`, not `HEDGE_FUND_LLM_MODEL`. Dropped now-unused `os` / `DEFAULT_MODEL` imports. | Two funds in one process may run different tiers; the record should say which answered. |
| `paths.py` | Module-level `Path.home()`-derived constants → injected root + call-time accessors (`set_root`, `root()`, `mandates_dir()`, …). | Data lives under `<BQ_HOME>/aihf`, like the rest of the app. Never `Path.home()`. |
| `llm/cache.py`, `data/cached.py` | Default dirs resolved at call time via `paths.*` instead of bound constants. | A root injected after import must still take effect. |
| `paper/deployed.py` | `root: Path = PAPER_DIR` → `root: Path | None = None` defaulting to `paths.paper_dir()`; `list_deployed` likewise. | Same reason; also avoids `Path(None)`. |
| `features/breakpoints.py` | Removed the Ken-French download (`requests`, `zipfile`, retry); `shared_breakpoints()` returns `None` when no local CSV exists. | The table describes **US** stocks; this port trades NSE names, so a US decile is worse than no size. Blind prompts already treat `size_percentile=None` as "omit size". |
| `data/protocol.py` | `FDClientError` → `DataClientError`. | The client is now the app's vendor router, not the FD API; the contract is identical. |
| `brokers/sim.py` | Added an optional `shares` argument to `SimBroker.__init__` and a `_charge_for` hook that prefers a cost model's `charge(side, quantity, price)`. | (a) The approval gate's compute step rebuilds the book in memory. (b) India's fees are percentages of turnover, which a per-share `commission()` cannot express; a model without `charge` keeps upstream behaviour exactly. |
| `pipeline/clock.py` | Added `set_clock_factory(factory)`; `clock_for` returns the factory's clock when one is installed. | The market clock (NSE/IST) is app policy. `clock_for` is called with no clock from deep inside `advance`, so a factory is the only way to substitute it once instead of at every call site. Left unset, upstream's New York clock is used unchanged. |
| `fund/spec.py::load_spec` | Refuses `rebalance: minute` and a `universe:` rule with a clear message. | Both are out of scope for the first NSE pass (ADR-052); failing at parse time tells the owner when they save, instead of mid-run. |
| `fund/spec.py` (`FundSpec.benchmark`) | Default `"SPY"` → `"NIFTY 50"`. | `SPY` cannot be resolved by this app; every default-constructed mandate would fail its first fetch. |
| `fund/example.yaml` | Rewritten for NSE: no `--universe`, `NIFTY 50` benchmark, US cost/CLI references removed. | The example is what an owner starts from. |
| `paper/__init__.py`, `backtesting/__init__.py`, `data/__init__.py` | Dropped exports of deleted modules. | Import hygiene. |

## Market adaptation: what stays in vendored code, and why

The market is app policy, so most of it lives in `bq/aihf/bridge/` rather than
here — a smaller upstream diff is easier to re-apply on an upgrade. Two things
deliberately stay:

- **`data/sessions.py` and the bar arithmetic in `pipeline/clock.py`** still
  name `America/New_York`. They are unreachable in this port: `clock_for` returns
  the bridge's IST clock whenever the app installs its factory, and intraday
  mandates are refused at validation. Keeping the New York code intact means
  upstream's own bar-grid logic still has its tests.
- **`brokers/costs.py`** still carries the US defaults (5 bps half-spread,
  $0.005/share, square-root impact). The app passes
  `bq/aihf/bridge/costs.py::indian_delivery_costs()` as the mandate cost model,
  which overrides the fees; the US defaults only apply to a spec built without
  one (i.e. a unit test).

If a later phase enables intraday NSE funds or a second market, the bar clocks
here will need the same factory treatment the daily clock now has.

