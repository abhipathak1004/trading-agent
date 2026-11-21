# Pinak

Intraday signal desk for Indian cash equities (NSE, MIS, flat by square-off).

Phase 1 builds the **signal desk**: ten agents that answer *"which stocks are worth
trading today, and why?"* — deterministic ranking, LLM only annotates or vetoes.

- Spec: `docs/PHASE-1-SIGNALS.md`
- Decisions: `docs/PHASE-1-DECISIONS.md`
- Open-source review: `docs/OPEN-SOURCE-REVIEW.md`

## Quick start

```bash
uv sync
uv run pinak doctor
uv run pinak universe
```

## Commands

| Command | Agent | Status |
|---|---|---|
| `pinak doctor` | — | done |
| `pinak universe` | A1 Universe | done |
| `pinak regime` | A2 Regime | done |
| `pinak quant` | A4 Quant | next |
| `pinak scan` | A3 Technical | planned |
| `pinak flow` | A5 Flow | planned |
| `pinak news` | A6 News/Event | planned |
| `pinak quality` | A7 Quality | planned |
| `pinak gate` | A8 Risk gate | planned |
| `pinak signals` | A9 Ranker + A10 Narrator | planned |
| `pinak replay` | whole desk | planned |

## Rules

- No third-party code in production without licence review.
- Every agent is deterministic unless explicitly marked LLM.
- Re-running on the same data must produce identical output.
- Data and reports live in `data/` and `reports/` and are never committed.
