# Pinak — Phase 1: Decisions & Specification

| Field | Value |
|---|---|
| Status | Draft for sign-off |
| Date | 21 Nov 2026 |
| Owner | Abhishek Pathak |
| Phase | 1 of 9 — **Signal desk** (code) |
| Scope | **Intraday, long and short, Indian cash equity (MIS)** |
| Source plans | `docs/plans/AI Trading System Implementation Plan v3.8.md`, `docs/plans/Indian Equity Trading Plan Rak.md` |

> **Purpose of this phase:** build the signal desk — the ten agents that answer *"which stocks are worth trading today, and why?"* The decisions below are frozen; the build plan is in `docs/PHASE-1-SIGNALS.md`.

---

## 1. What we are building

A personal **intraday Indian cash-equity trading system** that:

1. ingests validated intraday bars with point-in-time integrity,
2. runs **one** deterministic intraday strategy,
3. sizes and gates every trade deterministically,
4. simulates execution, charges, and accounting realistically,
5. runs forward in paper mode on a live intraday loop,
6. and measures whether an **AI research filter** adds value *after costs*.

Every position is opened and closed **within the same trading session**. Nothing is carried overnight.

**Frozen non-goals:** F&O, positional/swing/delivery trading, live money, web dashboard, value investing, YouTube research, multi-user, high-frequency trading.

> The source plans were written for swing/EOD. Intraday overrides their data layer, cadence, cost model, and evaluation design. Where they conflict, this document wins.

---

## 2. Governing principles (non-negotiable)

| # | Principle | Consequence |
|---|---|---|
| P1 | **LLMs propose; deterministic code sizes and approves.** | Risk gate always runs. `risk` and `broker` may never import `llm`. |
| P2 | **Point-in-time correctness.** | Bars, universe membership, and research are used only as they were available at that instant. |
| P3 | **Pessimistic fills.** | If one bar touches both stop and target, the stop wins. |
| P4 | **Flat by close.** | No position survives the square-off deadline. Overnight risk is zero by design. |
| P5 | **One execution and accounting engine.** | Backtest, replay, and paper share the same semantics. |
| P6 | **Halts persist.** | Restarting the process never auto-resumes trading. |
| P7 | **No trades on bad data.** | Stale/missing bars block new entries; exits stay armed. |
| P8 | **Everything is auditable.** | Fill → order → risk → intent → signal → data source, hash-chained. |

---

## 3. Position record (the output contract)

This is the record the system emits for every suggestion and every live position.

```python
class PositionSuggestion:
    id: str                      # uuid7
    trading_date: date
    created_at: datetime         # IST
    symbol: str
    exchange: str                # NSE
    instrument_type: str         # EQ (cash)
    side: Literal["BUY", "SELL"] # SELL = intraday short via MIS
    quantity: int                # whole shares
    entry_price: Decimal         # planned entry
    stop_loss: Decimal           # invalidation
    target_price: Decimal        # "sell price" for a long; buy-back for a short
    expiry_at: datetime          # TIME STOP — see §3.1
    risk_amount: Decimal         # ₹ at risk if stop hits
    risk_pct: float              # risk_amount / equity
    reward_risk: float           # (target - entry) / (entry - stop)
    confidence: float            # 0..1, P(target before stop within the session)
    confidence_level: str        # VERY_HIGH | HIGH | MODERATE | LOW
    expected_value_r: float      # after costs
    setup: str                   # e.g. ORB, VWAP_RECLAIM, GAP_AND_GO
    rationale: str
    status: str                  # SUGGESTED | OPEN | CLOSED | EXPIRED
    exit_price: Decimal | None
    exit_at: datetime | None
    exit_reason: str | None      # TARGET | STOP | TIME_STOP | SQUARE_OFF | MANUAL
```

### 3.1 The expiry / time-stop rule

"Expiry" is **not** a contract expiry. It is the deadline at which we abandon a trade that hit neither target nor stop.

```
expiry_at = min(entry_time + max_holding_minutes,
                square_off_time - safety_buffer)
```

- At `expiry_at`, if the position is still open → **exit at market** on the next bar.
- No new entries after `last_entry_time`.
- If the strategy has a signal-decay exit, it may close earlier than `expiry_at`.

**Default timings (change if you want):**

| Setting | Default |
|---|---|
| First entry allowed | 09:20 IST (after the opening auction settles) |
| Last new entry | 14:30 IST |
| Max holding per trade | 90 minutes |
| Broker square-off | 15:20 IST |
| Safety buffer | 5 minutes → hard expiry cap 15:15 IST |

---

## 4. Phase plan and gates

Phase 2 is a timeboxed **open-source assessment**, never a production dependency.

| Phase | Name | Deliverable | Exit gate |
|---|---|---|---|
| **1** | **Signal desk** | Ten-agent signal pipeline (A1–A10) producing ranked intraday ideas with full evidence | ≤ 5 ideas/day, byte-identical replay, useful with the LLM off |
| **2** | **Open-source assessment** (3–5 days) | Reuse / adapt / replace log for the repos in `OPEN-SOURCE-REVIEW.md` + PKScreener + Hummingbird | Decision recorded. **No third-party code in production without review.** |
| **3** | **Foundations & intraday data** | Config, calendar, intraday bar ingest, validation, point-in-time store | Same inputs → same features; bad data cannot trade |
| **4** | **Deterministic baseline + backtest** | Indicators, one intraday strategy, sizing, risk, Indian cost model, walk-forward, metrics | Explainable without an LLM; replay deterministic; accounting reconciles |
| **5** | **Paper execution & accounting** | OMS, paper broker, fills, paise ledger, intraday charges, reconciliation, restart recovery | Golden contract-note match; no look-ahead; halts persist |
| **6** | **Forward paper loop** | Live intraday CLI loop, scheduler, journal, reports, alerts | N stable sessions; stability judged separately from profitability |
| **7** | **AI shadow mode** | News/filings ingest, structured events with citations, one advisor, shadow vetoes | Event recall and extraction accuracy acceptable |
| **8** | **Controlled AI experiment** | A/B/C books, counterfactuals, cost deduction | Keep AI only if a predefined objective improves after costs |
| **9** | **Hardening / live** | *Deferred* | Separate compliance gate (SEBI algo framework) |

### Phase 2 in detail — open-source assessment

Targets:

1. RakshaQuant (if a repository exists)
2. `pradeepsiddappa/indian-trading-agent` (Apache-2.0)
3. `hopit-ai/india-trade-cli` (MIT)

For each: review licence and dependencies, pin a reviewed commit, run its tests, inspect data/strategy/risk/fill/accounting/exit code, and produce a **reuse / adapt / replace** log. If adaptation costs more than a small replacement, replace it. Output is knowledge, not a runtime dependency.

See `docs/OPEN-SOURCE-REVIEW.md` for findings and the adopt/adapt/ignore list.

---

## 5. Decision register

| ID | Decision | Recommended default | Notes |
|---|---|---|---|
| **D1** | Universe | Top ~150 NSE names by 20-day traded value. Filters: price ≥ ₹50, ADV ≥ ₹10 Cr, exclude ASM/GSM, exclude results/board meeting day, exclude circuit-locked | Intraday needs liquidity above all else |
| **D2** | Strategy | **Opening Range Breakout (ORB)** — break of the 09:15–09:30 range, volume confirmation, ATR stop, measured-move target | Alternatives: VWAP reclaim, gap-and-go, first-hour pullback. One only. |
| **D3** | Horizon / product | Intraday only, MIS, flat by 15:20 IST | No overnight, no delivery |
| **D4** | Paper capital | ₹10,00,000, single intraday bucket | |
| **D5** | Risk | 0.25–0.5% per trade; max 3 concurrent; max 5 trades/day; daily loss limit 2%; 3 consecutive losses → stop for the day; no entries after 14:30 | Intraday costs are a large share of edge — keep risk tight |
| **D6** | Cost model | Versioned `charges.yaml` with effective dates: MIS brokerage, STT (sell side), exchange txn, GST, SEBI, stamp. Slippage 5–15 bps + spread. **Every rate marked "verify".** | Intraday cost drag is the #1 killer of edge |
| **D7** | Intraday data | **Backtest:** HuggingFace `xxparthparekhxx/indian-stock-market-minute-data` — 3,182 NSE symbols, 1-minute, 2022-01-03 → 2026-11-14, MIT, Parquet, queried via DuckDB `hf://`. **Recent/forward:** OpenChart (free, no auth, NSE charting endpoints). **EOD/calendar/corporate actions:** nselib + jugaad-data. **Fallback:** yfinance 5m. **No broker required.** | See §5.1 for caveats |
| **D8** | Benchmark | NIFTY 50 intraday return; plus a "no-trade" baseline | |
| **D9** | Evaluation | Walk-forward; min 300 trades; net expectancy in R after 2× costs; win rate, avg R, MAE/MFE, time-to-target, trades/day, max intraday drawdown | Acceptance: net expectancy > 0 at 2× slippage **and** beats no-trade |
| **D10** | AI scope | Shadow only in Phase 7; one reasoning model; timeout + token budget; cited structured events; **veto or lower only** | Token ledger from day one |
| **D11** | Interface | CLI + Markdown/JSON reports + SQLite/Parquet. No web UI | |
| **D12** | Stack | Python 3.12, uv, Polars, DuckDB, PyArrow, NumPy, Pydantic v2, httpx, structlog, pytest, Hypothesis, ruff, mypy | No React, no Rust, no Numba until benchmarked |
| **D13** | Storage | SQLite WAL for state/audit; Parquet for intraday bars; money as **int paise**; timestamps **int64 ns UTC** | |
| **D14** | Repo layout | `src/pinak/{core,infra,data,compute,strategies,backtest,broker,risk,paper,agent,orchestration,cli}` | Boundaries enforced by import-linter |
| **D15** | "Autonomous" | Auto paper-trading, no per-trade approval; deterministic risk still gates every order; live money out of scope | |
| **D16** | Time stop | See §3.1. Exit at market when `expiry_at` is reached | |

### 5.1 Data plan (D7 detail)

**Backtest (Phase 4)**
- Primary source: HuggingFace `xxparthparekhxx/indian-stock-market-minute-data` — 1-minute NSE bars, 2022-01-03 → 2026-11-14.
- Access via DuckDB over `hf://...` Parquet; no full 13.9 GB download needed.
- Copy what we use into our own Parquet layout with source and ingestion timestamps.
- Record these biases on every backtest: survivorship bias, monthly refresh lag, split/bonus adjusted but **not** dividend adjusted, gaps not filled, zero-volume candles.

**Forward paper (Phase 6)**
- OpenChart polling on the watchlist and open positions only.
- Tag every fill `fill_quality=DELAYED` unless the source is confirmed real-time.
- A free broker API (Upstox / Fyers / Angel One) is optional and plugs in behind the same `DataProvider` interface.

**Reference data**
- `nselib` / `jugaad-data`: bhavcopy, holiday calendar, corporate actions, delivery %.
- Our own archive starts on day one and becomes the long-term source.

**Rules**
- Every provider sits behind one `DataProvider` interface.
- Contract tests against recorded fixtures.
- Circuit breaker + token-bucket rate limit per host.
- Stale data blocks new entries; exits stay armed.

---

## 6. What Phase 1 will have produced

No trading code. Seven artifacts:

1. Source plans preserved in `docs/plans/`.
2. This decision document with every D-item answered.
3. Signed-off phase gates.
4. One-page **strategy spec** (ORB entry, stop, target, time stop, invalidation).
5. One-page **risk spec** (limits, sizing, halt conditions).
6. One-page **data spec** (intraday sources, fields, validation, point-in-time rules).
7. One-page **evaluation spec** (metrics, splits, thresholds).

---

## 7. Open questions (these block Phase 3)

1. **Intraday data — resolved.** No broker needed: backtest on the HuggingFace 1-minute dataset, forward paper via OpenChart. Confirm you accept the known biases (survivorship, monthly refresh lag).
2. **RakshaQuant** — repository URL, or just a name? The other two Phase 2 targets are public.
3. **LLM keys** — Gemini and/or DeepSeek today?
4. **Machine** — confirm target is this **M4 Pro, 12 cores, 24 GB** (plan assumed M5 Pro).
5. **Capacity** — solo build? Hours per week?
6. **Strategy** — ORB acceptable, or prefer VWAP reclaim / gap-and-go / pullback?
7. **Timings** — accept the defaults in §3.1 (last entry 14:30, 90-min hold, square-off 15:20)?

---

## 8. Traceability

| Pinak decision | Rak plan | BharatQuant v3.8 |
|---|---|---|
| Deterministic-first, AI shadow | §4 Phases 0–6 | §12.3, §13.3 |
| LLM never sizes/approves | §1, §5 | §3, §9, §18.3, ADR-009 |
| Intraday horizon, MIS, square-off | — (excluded) | §12.1, §17 |
| Pessimistic fills | §4 Phase 3 | §16.4 |
| Indian charges | §4 Phase 3 | §17 |
| CLI + reports before UI | §3 | §7 |
| Money as int paise | — | §6.4, ADR-011 |
| Forward-only validation of AI | §4 Phase 5–6 | §13.3, ADR-019 |
