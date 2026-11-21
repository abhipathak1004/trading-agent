# Open-Source Review — What to Adopt, Adapt, or Ignore

| Field | Value |
|---|---|
| Date | 21 Nov 2026 |
| Purpose | Assess external projects against Pinak's Phase 1 spec |
| Verdict | **References, not foundations.** Nothing here replaces our architecture. Several patterns improve it. |

---

## 1. `pradeepsiddappa/indian-trading-agent`

**What it is:** an Indian-market fork of TauricResearch/TradingAgents. Apache-2.0. A multi-agent LLM research pipeline (LangGraph): market/social/news/fundamentals analysts → bull vs bear debate → research manager → trader → risk debate → portfolio manager. Ships a FastAPI backend, Next.js terminal, SQLite.

**Horizon:** daily / next-day. The repo itself lists "real-time intraday signal loop" as *future* work. It is **not** an intraday system.

**What is genuinely good:**
- **Brier score + reliability diagram** for the recommender's stated probability. Honest calibration, not just hit rate.
- **Shadow trades** — auto-records every strong pick whether or not the user acted, then backfills P&L. Splits into tracked vs skipped to prove whether filtering helps.
- **Wilson lower bound** for signal win rates, with a minimum-sample rule before any weight change.
- **Market regime classifier** (BULL / BEAR / SIDEWAYS / HIGH_VOL) and per-regime signal performance.
- **Pre-trade filters:** FII/DII flow, earnings/event calendar, sector concentration cap (3 positions / 30% per sector).
- **Paper trading + historical replay** as a separate engine from the LLM analysis.

**What is weak or wrong for us:**
- LLM is the decision-maker. Contradicts our P1 (LLM proposes, code approves).
- LangGraph + 10 agents per stock is slow and expensive — unusable on an intraday clock.
- yfinance-first data, no real intraday feed.
- Signal weights auto-tuned from live outcomes — high overfitting risk; we would gate any tuning behind walk-forward validation.
- Next.js UI, not our CLI-first choice.

**Use:** reference for **evaluation and calibration** (Brier, Wilson, shadow trades, regime splits) and for **pre-trade filters** (FII/DII, events, sector cap). Do not copy the pipeline.

---

## 2. `hopit-ai/india-trade-cli` ("Vibe Trading")

**What it is:** MIT. An agentic Indian-market platform: 7 analyst agents (technical, fundamental, options, news, sentiment, sector, risk) → scorecard → multi-round bull/bear debate → fund-manager synthesis → **3 risk-profiled trade plans** (aggressive / neutral / conservative) with entry, stop, targets, and sizing. CLI + Electron macOS app + FastAPI. Brokers: Fyers, Zerodha, Angel One, Upstox, mock. Live and paper modes. 58-strategy library, options GEX/IV/Greeks, VaR/CVaR, walk-forward, Telegram.

**Horizon:** per-stock analysis, not a fast intraday loop. But it does have the execution and trade-plan plumbing we need.

**What is genuinely good:**
- **Trade-plan schema**: entry, stop, multiple targets, sizing, per risk profile. Directly validates our `PositionSuggestion`.
- **Broker abstraction** with five adapters including a mock — a clean pattern for our `DataProvider` / `BrokerAdapter` split.
- **Mock/demo mode** (`trade --no-broker`) using delayed data — exactly our paper-first stance.
- **Fyers confirmed as the best free Indian data API** (live quotes, options chain, WebSocket, historical). Zerodha Connect is paid for the same.
- **VIX-scaled position sizing** (100% → 85% → 65% → 50% as VIX rises).
- **Risk metrics**: VaR/CVaR, concentration, portfolio Greeks.
- **SEBI static-IP requirement** for live orders, already surfaced in the UX.

**What is weak or wrong for us:**
- 7-agent debate per symbol is minutes-long and token-heavy — wrong cadence for intraday.
- LLM produces the verdict; we require deterministic sizing and approval.
- Options/F&O focus is out of our scope.
- Electron app is irrelevant to a CLI-first MVP.

**Use:** reference for the **trade-plan record**, **broker/data adapter interface**, **mock mode**, **VIX sizing**, and **compliance notes**. Do not copy the agent stack.

---

## 3. Medium article — the nine repositories

Now readable via the Freedium mirror. They are almost entirely **broker connectivity and market-data plumbing**, not strategy. That is itself informative: the Indian OSS ecosystem is strong on adapters and weak on validated strategies.

| # | Repo | What it is | Relevance to Pinak | Verdict |
|---|---|---|---|---|
| 1 | `TheHardeep/fenix` | Python multi-broker library, 15+ Indian brokers, common order/position interface | The mature version of our future `BrokerAdapter`. Not needed while paper-only | **Adopt later** (Phase 9) |
| 2 | `SU1199/uniBroker` | Go unified broker API, work in progress | Wrong language, immature | Ignore |
| 3 | `aeron7/nsepython` | Unofficial NSE wrapper: quotes, option chain, indices, FII/DII | Alternative to nselib/jugaad-data for reference data and FII/DII. Check licence (reported GPL) | **Optional adapter** |
| 4 | `jerinisready/paper-trade-broker` | Django paper-trading backend: orders, funds, positions, status machine | Reference for our OMS state machine and funds ledger. Separate service, so not a dependency | **Adapt concepts** |
| 5 | `QuantConnect/Lean.Brokerages.Zerodha` | LEAN (C#) Zerodha plugin | Too heavy, C#, no Indian intraday venue model — the same reason we rejected Nautilus | Ignore |
| 6 | `Prostocks/StarApi-py` | Broker-specific wrapper | Only if using Prostocks | Ignore |
| 7 | `Apurv-Salunke/india-stocks-api` | Multi-broker data access, unstable `dev` branch | Small, concept only | Ignore |
| 8 | `RAKESHKRAJEEV90/nse-api-package` | Node.js NSE wrapper | Wrong language | Ignore |
| 9 | `Sampad-Hegde/Bharat-SM-Data` | Indian market data collection pipeline | Reference for validation and storage design | **Adapt concepts** |

**What this changes:**

- Adds two optional Phase 2 targets: `fenix` (broker abstraction, relevant only at Phase 9) and `paper-trade-broker` (OMS state machine).
- Confirms `nsepython` as a fallback data adapter, subject to a licence check.
- Confirms LEAN is **not** a candidate.
- Does **not** change the core: none of these provide an intraday strategy, risk engine, or evaluation method.

---

## 4. Side-by-side against Pinak

| Dimension | Pinak (ours) | indian-trading-agent | india-trade-cli |
|---|---|---|---|
| Horizon | **Intraday** | Daily / next-day | Per-stock, not intraday loop |
| Decision authority | **Deterministic** | LLM | LLM |
| AI role | Veto / annotate only | Decides | Decides |
| Orchestration | Lightweight DAG | LangGraph, 10 agents | 7 agents + debate |
| Data | HF 1-min + OpenChart | yfinance / NSE scrape | Fyers (free) |
| Execution | Paper only | Paper + read-only Kite | Paper + live |
| Interface | CLI + reports | Next.js | CLI + Electron |
| Licence | — | Apache-2.0 | MIT |

**Conclusion:** both are **LLM-decision-first, daily-horizon** systems. Ours is **deterministic-first, intraday**. They are useful for parts, not for the whole.

---

## 5. Concrete changes to our flow

### Adopt (reimplement the pattern, not the code)

| # | Pattern | Where it lands |
|---|---|---|
| A1 | **Brier score + reliability diagram** | D9 evaluation spec; `confidence` field must be scored, not just displayed |
| A2 | **Shadow trades / counterfactual book** | Phase 8; split tracked vs skipped |
| A3 | **Wilson lower bound + minimum sample** | Confidence and signal stats; no weight change below N trades |
| A4 | **Regime classifier** (BULL/BEAR/SIDEWAYS/HIGH_VOL) | Pre-trade gate; evaluation split by regime |
| A5 | **Event calendar filter** (results, RBI, FOMC, expiry) | Risk gate: no new entry into known event windows |
| A6 | **FII/DII flow filter** | Daily bias / size throttle |
| A7 | **Sector concentration cap** | D5 risk limits |
| A8 | **VIX-scaled sizing** | D5 sizing formula |
| A9 | **Trade-plan schema** (entry, stop, targets, size, profile) | Extend `PositionSuggestion` with `target_2` and `risk_profile` |
| A10 | **Broker/data adapter + mock mode** | `DataProvider` and `BrokerAdapter` interfaces |
| A11 | **SEBI static-IP note** | Phase 9 checklist |
| A12 | **Fenix multi-broker abstraction** | Phase 9 only; reference for the adapter surface |
| A13 | **paper-trade-broker OMS state machine** | Phase 5 order lifecycle and funds ledger |

### Adapt (use, but change)

| # | Pattern | Change |
|---|---|---|
| B1 | Multi-agent debate | Collapse to **one advisor + optional critic**, and only in Phase 7 shadow mode |
| B2 | Auto-tuning weights from live outcomes | **Reject live auto-tune.** Any tuning goes through walk-forward validation and a human accept step |
| B3 | Agent memory (BM25 + decay) | Defer; not needed for the MVP |
| B4 | 58-strategy library | Reference only; we build one strategy first |
| B5 | Options analytics | Out of scope (no F&O) |

### Ignore

- LangGraph orchestration and the 10/7-agent debate pipelines.
- Next.js / Electron front-ends.
- Live order placement.
- Options GEX / IV / Greeks.

### Replace

- Nothing wholesale. Neither repo is a foundation candidate.

---

## 6. Impact on the phase plan

Phase 2 should be broadened from "RakshaQuant assessment" to **open-source assessment**, with three targets:

1. RakshaQuant (if a repo exists)
2. `pradeepsiddappa/indian-trading-agent`
3. `hopit-ai/india-trade-cli`

Timebox stays 3–5 days. Output stays a reuse / adapt / replace log. Rule stays: no third-party code in production without review and licence check.

**One decision this changes:** D7 now has a confirmed free option — **Fyers** for live quotes and historical data, if we ever want a stable forward-paper feed. Still no broker needed for backtest.

---

## 7. Bottom line

- **Best ideas to steal:** Brier calibration, shadow trades, Wilson bounds, regime splits, event/FII/DII/sector filters, VIX sizing, trade-plan schema, adapter+mock pattern, OMS state machine.
- **Worst ideas to avoid:** LLM as final decision-maker, per-stock debate on an intraday clock, auto-tuning from live P&L, yfinance-only data, heavyweight engines (LEAN/Nautilus).
- **None of the eleven projects reviewed replaces ours.** They confirm the shape of the parts we still have to build.
