# Pinak — Phase 1: The Signal Desk

| Field | Value |
|---|---|
| Status | Building |
| Date | 21 Nov 2026 |
| Phase | 1 of 9 — **Signal generation** |
| Scope | Intraday, long and short, Indian cash equity (MIS) |
| Output | A ranked, evidence-backed list of tradable ideas for *today* |

> **What changed:** Phase 1 is no longer "write a decisions document". The decisions are made. Phase 1 now **builds the signal desk** — the thing that answers *"which stocks are worth trading today, and why?"*

---

## 1. The question Phase 1 answers

Every trading morning, before 09:20 IST, the system must answer:

1. **Should we trade at all today?** (market regime + daily verdict)
2. **Which symbols are eligible?** (liquidity, bans, surveillance, circuit)
3. **Which of them have a setup right now?** (technical)
4. **Is the setup statistically credible?** (quant)
5. **Is money actually moving into it?** (flow)
6. **Is there a reason to move — or a reason to stay away?** (news/events)
7. **Is it a trap?** (quality)
8. **Is it allowed?** (risk gate)
9. **Which 3–5 are best, and at what price?** (ranker)
10. **Why?** (narrator)

Each question is one agent. No agent does another agent's job.

---

## 2. Architecture

```
                    A0  ORCHESTRATOR (DAG, one run per session)
                                   │
   ┌───────────────┬───────────────┼───────────────┬───────────────┐
   │  EOD (T-1)    │  PRE-OPEN     │  OPEN 09:15   │  LIVE 09:20+  │
   │  18:00        │  08:45        │               │               │
   ├───────────────┼───────────────┼───────────────┼───────────────┤
   │ A1 Universe   │ A6 News/Event │ A3 Technical  │ A3 re-ranks   │
   │ A2 Regime     │ A7 Quality    │ A4 Quant      │ A8 gate       │
   │ A4 Quant      │ A5 Flow       │ A8 Risk gate  │ A9 ranker     │
   │ A5 Flow       │               │ A9 ranker     │               │
   └───────────────┴───────────────┴───────────────┴───────────────┘
                                   │
                          A10 NARRATOR (LLM)
                        annotate · veto · downweight
                                   │
                    signals.json  +  signals.md
```

**Hard rule (unchanged):** the LLM cannot add a candidate, cannot size, cannot raise confidence. It may only annotate, veto, or downweight. Everything before A10 is deterministic and reproducible.

---

## 3. The ten agents

Every agent has: an input contract, an output contract, a failure mode, and a reason code set.

### A1 — Universe ("what can we trade today?")

| | |
|---|---|
| **Input** | NSE bhavcopy, 20 trading days |
| **Output** | `TradableSymbol[]` — symbol, close, ADV20 ₹Cr, turnover, ATR%, delivery %, history depth |
| **Gates** | series = EQ · price ≥ ₹50 · ADV20 ≥ ₹10 Cr · turnover ≥ ₹5 Cr · ≥ 15 days history · not SME · not in ban/ASM/GSM · not circuit-locked |
| **Failure** | Missing bhavcopy → treat as holiday, walk back. Stale (> 3 sessions) → block the run |
| **Reuse** | Repo 1 scanner filters · PKScreener `minimumvolume` / index lists |

### A2 — Regime ("should we trade at all?")

| | |
|---|---|
| **Input** | NIFTY 50 daily, India VIX, advance/decline breadth |
| **Output** | `Regime` = BULL / BEAR / SIDEWAYS / HIGH_VOL + `size_multiplier` + `max_new_positions` + verdict GREEN / YELLOW / RED |
| **Gates** | RED → zero new entries. YELLOW → half size, 2 trades. GREEN → full |
| **Reuse** | **Repo 1** regime classifier + Daily Verdict decision table (direct port) |

### A3 — Technical ("is there a setup right now?")

| | |
|---|---|
| **Input** | 1m / 5m / 1d bars, previous day H/L/C, opening range |
| **Output** | `SetupCandidate[]` — setup name, direction, entry trigger, stop, target, invalidation |
| **Setups (v1)** | ORB-15 · VWAP reclaim · gap-and-go · first-hour pullback |
| **Features** | opening range, VWAP ± bands, ATR(14), RVOL vs 20d, S/R, volume-profile POC, gap %, prev-day levels |
| **Reuse** | **Hummingbird** ORB spec (RVOL ≥ 100%, ATR stop) · **PKScreener** intraday scanners |

### A4 — Quant ("is it statistically credible?")

| | |
|---|---|
| **Input** | Daily + intraday history, index and sector returns |
| **Output** | `QuantProfile` — RS rank, beta, realized vol, vol regime, gap-fill rate, time-of-day edge, MR z-score, correlation cluster |
| **Key number** | Per-symbol, per-setup **historical hit rate** → feeds calibrated confidence |
| **Reuse** | Repo 2 walk-forward harness · repo 1 Wilson lower bound |

### A5 — Flow ("is money moving in?")

| | |
|---|---|
| **Input** | FII/DII net, bulk/block deals, delivery %, volume surge |
| **Output** | `FlowBias` — score −2..+2 per symbol, plus market-wide bias |
| **Reuse** | **Repo 1** FII/DII filter thresholds · `nsepython` bulk/block endpoints |

### A6 — News & Events ("reason to move, or reason to avoid?")

| | |
|---|---|
| **Input** | RSS (ET Markets, Moneycontrol, Business Standard, Mint, Google News), corporate calendar |
| **Output** | `EventFlag[]` — severity BLOCK / CAUTION / CATALYST, headline, source, timestamp |
| **Calendar** | earnings, board meeting, corp action, RBI, FOMC, Budget, F&O expiry |
| **Reuse** | **Repo 1** RSS aggregator + earnings/economic calendar filter |
| **LLM** | Optional here: headlines → structured event with citation. Never a score |

### A7 — Quality / Trap ("is this a trap?")

| | |
|---|---|
| **Input** | Float, market cap, promoter holding, listing age, price history |
| **Output** | `TrapScore` 0..1 + disqualifiers |
| **Flags** | recent listing, penny, low float, repeated upper circuits, 52w extreme, T2T, operator-pattern |
| **Reuse** | Repo 2 fundamentals agent, stripped to trap detection only |

### A8 — Risk Gate ("is this allowed?")

| | |
|---|---|
| **Input** | All of the above + account state |
| **Output** | ACCEPT / REJECT with reason codes |
| **Vetoes** | spread, ATR band, event blackout, sector cap (3 / 30%), concurrent cap (3), daily trades (5), daily loss (2%), 3-loss halt, VIX cap |
| **Reuse** | **Repo 2** VIX-scaled sizing · **Repo 1** sector concentration checker |

### A9 — Ranker ("which 3 today, and at what price?")

| | |
|---|---|
| **Input** | Every accepted candidate + all evidence |
| **Output** | Ranked `SignalCandidate[]` with full position plan |
| **Score** | `w·[setup, rvol, rs, flow, regime_fit] − p·[event, trap, spread]` |
| **Confidence** | **Wilson lower bound** of the historical hit rate for that setup/regime bucket — never an LLM number |
| **Sizing** | risk % → quantity, clipped by liquidity and sector caps |
| **Reuse** | **Repo 1** recommendation engine · **Repo 2** trade-plan schema |

### A10 — Narrator ("explain it, and object if needed")

| | |
|---|---|
| **Input** | Top candidates + evidence bundle |
| **Output** | 2–4 sentence rationale per idea; optional VETO or DOWNWEIGHT with reason |
| **Constraints** | no new candidates · no sizing · no confidence increase · token budget · hard timeout · failure = no-op |
| **Reuse** | Repo 2 trade-plan prose |

---

## 4. Scoring and confidence

```
score = 0.30·setup_quality
      + 0.20·rvol_z
      + 0.15·rs_rank
      + 0.15·flow
      + 0.10·regime_fit
      − 0.25·event_risk
      − 0.20·trap_score
      − 0.10·spread_penalty
```

- Every weight lives in `config/weights.toml`, versioned. No weight changes without a walk-forward run.
- `confidence = wilson_lower_bound(hit_rate, n)` over the matching bucket, requiring `n ≥ 30`.
- Below `n = 30`, confidence is capped at `MODERATE` and labelled `UNCALIBRATED`.

**Output contract** — one row per idea:

```json
{
  "id": "01J...",
  "trading_date": "2026-11-24",
  "symbol": "TATAMOTORS",
  "side": "BUY",
  "setup": "ORB_15",
  "quantity": 120,
  "entry_price": 712.40,
  "stop_loss": 706.10,
  "target_price": 723.80,
  "expiry_at": "2026-11-24T11:05:00+05:30",
  "risk_amount": 756.00,
  "risk_pct": 0.00076,
  "reward_risk": 1.81,
  "confidence": 0.58,
  "confidence_level": "MODERATE",
  "score": 0.71,
  "evidence": {
    "technical": {...},
    "quant": {...},
    "flow": {...},
    "news": [...],
    "quality": {...}
  },
  "vetoes": [],
  "rationale": "..."
}
```

---

## 5. Reuse map — what comes from where

| Component | Source | What exactly | Licence |
|---|---|---|---|
| Regime classifier | `pradeepsiddappa/indian-trading-agent` | BULL/BEAR/SIDEWAYS/HIGH_VOL thresholds | Apache-2.0 |
| Daily verdict | same | GREEN/YELLOW/RED table + size multipliers | Apache-2.0 |
| FII/DII filter | same | ±1.5 / ±1.0 thresholds, mixed-bias rule | Apache-2.0 |
| Event calendar | same | RBI/Budget/FOMC/expiry penalties | Apache-2.0 |
| Sector cap | same | 3 positions / 30% capital | Apache-2.0 |
| Signal scoring | same | weighted-signal recommender pattern | Apache-2.0 |
| Calibration | same | Brier, reliability diagram, Wilson LB, shadow trades | Apache-2.0 |
| Trade-plan schema | `hopit-ai/india-trade-cli` | entry/stop/targets/size/profile | MIT |
| Broker adapter + mock | same | `DataProvider` / `BrokerAdapter` split | MIT |
| VIX sizing | same | 100→85→65→50 % | MIT |
| Morning brief | same | FII/DII + breadth + news digest | MIT |
| ORB spec | `JittoJoseph/Hummingbird-Project` | RVOL ≥ 100 %, ATR stop, 09:15–09:20 range | check |
| Intraday scanners | `pkjmesra/PKScreener` | volume / momentum / NR4 / breakout logic | check |
| NSE data | `aeron7/nsepython` | FII/DII, bulk/block, ban list, circuit | **GPL — lazy import only** |
| Bhavcopy / calendar | `nselib`, `jugaad-data` | EOD, holidays, corp actions | check |
| 1-min history | HuggingFace dataset | 2022 → 2026 backtest | MIT |

**Licence rule:** GPL code is never linked into Pinak. `nsepython` is an optional, lazily-imported adapter with a raw-HTTP fallback.

---

## 6. Build order — one at a time

| # | Component | Depends on | Status | Done when |
|---|---|---|---|---|
| **1** | **Foundation** — repo, config, models, providers, cache, CLI | — | ✅ done | `pinak doctor` runs green |
| **2** | **A1 Universe** | 1 | ✅ done | prints today's tradable list from real bhavcopy |
| **3** | **A2 Regime** | 1 | ✅ done | prints regime + daily verdict |
| **4** | **A4 Quant** | 1, 2 | next | per-symbol stats table |
| **5** | **A3 Technical** | 1, 2 | planned | setup candidates with geometry |
| **6** | **A5 Flow** | 1 | planned | flow bias per symbol |
| **7** | **A6 News/Event** | 1 | planned | event flags with citations |
| **8** | **A7 Quality** | 1 | planned | trap flags |
| **9** | **A8 Risk gate** | 2–8 | planned | accept/reject with reason codes |
| **10** | **A9 Ranker + report** | all | planned | `signals.md` for a real day |
| **11** | **A10 Narrator** | 10 | planned | rationale + veto, budget-capped |
| **12** | **Calibration harness** | 10 | planned | Brier / Wilson on 6 months of history |

Each step ships with tests and a CLI command. No step starts before the previous one runs end-to-end on real data.

---

## 7. CLI surface

```
pinak doctor                      # environment + data health
pinak universe --as-of 2026-11-21 # A1
pinak regime                      # A2
pinak quant --top 50              # A4
pinak scan --setup orb15          # A3
pinak flow                        # A5
pinak news --symbols ...          # A6
pinak quality                     # A7
pinak gate                        # A8
pinak signals                     # A9 + A10 → signals.json / signals.md
pinak replay --from ... --to ...  # backtest the whole desk
```

---

## 8. What "good" looks like

The desk is working when, for a real trading day:

1. It runs unattended from 08:45 to 14:30 with no manual input.
2. It produces ≤ 5 ideas, each with entry, stop, target, expiry, size, risk, and confidence.
3. Every idea can be traced to the exact evidence that produced it.
4. Re-running on the same data produces byte-identical output.
5. The LLM being unavailable changes only the prose, never the list.
6. After 6 months of replay, confidence buckets are calibrated (Brier ≤ 0.20).

Point 5 is the one that matters. **The desk must be useful with the AI switched off.**
