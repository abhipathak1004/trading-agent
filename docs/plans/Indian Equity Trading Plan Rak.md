# Indian Equity Trading Plan

## RakshaQuant\-based phased MVP

### Objective

Build a reliable Indian cash\-equity paper\-trading system, prove one deterministic swing strategy, and measure whether an AI research filter improves results after costs.

This revision adopts RakshaQuant as the candidate implementation foundation. It replaces planned custom infrastructure, not our strategy, data\-quality standards or validation requirements. Adoption remains conditional on code review and testing. No integration or live deployment is implied by this plan.

## 1\. Scope and boundaries

- Personal research and paper trading initially.
- Long\-only cash\-equity swing trading; no F&O or intraday MVP.
- One fixed liquid\-stock universe and one precisely defined strategy.
- Deterministic position sizing, portfolio risk and execution.
- AI initially annotates candidates in shadow mode; later it may veto entries within a controlled experiment.
- AI cannot invent trades, resize positions, override risk limits or control exits.
- Live trading requires a separate approval, broker integration and operational/compliance assessment.
- Estimates below assume focused development. Forward evidence collection is separate from implementation time.

## 2\. Target architecture

Validated Indian EOD data → frozen deterministic strategy → optional advisor assessment → deterministic risk gate → simulated orders and fills → position and exit management → event records → evaluation reports.

### Ownership

**Our responsibility:** trading hypothesis, universe, strategy rules, trusted data, risk policy, evaluation design and adoption decisions.

**RakshaQuant reuse candidates:** paper execution, risk\-control plumbing, event records, portfolio projections, advisor resilience, counterfactual tracking and operational interfaces.

**Separate research module:** official announcement/news ingestion and source\-grounded event extraction.

All reuse candidates must be confirmed against the selected repository version. Features discussed in the repository documentation are not treated as independently verified capabilities.

## 3\. What RakshaQuant replaces

| Previously planned component | Revised approach | Acceptance requirement |
| --- | --- | --- |
| Custom paper\-order engine | Reuse and adapt RakshaQuant | Correct order lifecycle, idempotency and reconciliation |
| Custom fill and exit simulator | Reuse one engine rather than build a competing simulator | Conservative fills, gap handling, time exits and overnight positions |
| Custom risk\-control plumbing | Reuse with our limits | Hard enforcement, aggregate reservations and persistent halts |
| Custom journal and portfolio projections | Reuse event\-store approach | Reproducible state and complete decision trace |
| Advisor budgets, retries and failure handling | Reuse where suitable | Explicit timeout, invalid\-output and budget\-exhaustion policies |
| Counterfactual tracking | Reuse or extend | Rejected trades simulated under identical execution rules |
| Initial custom React dashboard | Defer; use existing interfaces first | Sufficient visibility for daily operation and diagnosis |
| Indian data pipeline | Validate existing adapters; extend or replace as necessary | Point\-in\-time integrity, corporate actions and stale\-data blocking |
| Strategy scanner | Keep our rules; implement through an appropriate interface | Deterministic and reproducible signals |
| Performance evaluation | Extend existing reporting | Net expectancy, benchmarks, stress tests and forward evidence |
| Live broker integration | Not replaced; deferred | Separate safety and compliance gate |

## 4\. Phased implementation

### Phase 0: Freeze the experiment and assess RakshaQuant

**Estimate: 3–5 focused days**

**Tasks**

- Define universe, holding horizon, entry, stop, target and time\-exit rules.
- Specify paper capital, risk limits and benchmark.
- Write the edge hypothesis and expected failure regimes.
- Pre\-register comparison rules, costs and retain/remove criteria.
- Review repository licence, dependencies, configuration and extension points.
- Pin a reviewed version; run tests and the bundled demo.
- Inspect data, strategy, risk, fill, accounting and exit implementations.
- Produce a reuse/adapt/replace decision log.

**Deliverables:** frozen experiment specification, repository assessment and integration map.

**Gate:** adopt only if the core is understandable, testable and adaptable without extensive rewriting. A synthetic demo proves setup readiness, not trading performance.

### Phase 1: Validate Indian EOD data and swing support

**Estimate: 1–2 weeks**

**Tasks**

- Connect and validate EOD OHLCV, instruments and trading calendar.
- Preserve raw inputs and availability/ingestion timestamps.
- Handle corporate actions and symbol changes.
- Add missing\-data, duplicate and outlier checks.
- Block new entries when required data or calendar information is invalid.
- Confirm overnight position persistence, next\-session entries, gap exits and time exits.
- Adapt scheduling to swing trading rather than copying a daily operating timetable.
- Use historical membership where historical universe\-level claims are made.

**Deliverables:** reproducible dataset, data\-health report and swing lifecycle tests.

**Gate:** identical inputs produce identical features; missing or invalid inputs cannot silently produce trades.

### Phase 2: Implement one deterministic baseline

**Estimate: 1 week**

**Tasks**

- Implement the frozen strategy through the selected interface.
- Disable AI influence.
- Configure sizing, exposure, liquidity and capital limits.
- Save every candidate, rejection and decision.
- Test entry triggers, stops, targets and expiry.

**Deliverables:** explainable scanner and baseline strategy configuration.

**Gate:** every candidate and trade is explainable without an LLM, and replay is deterministic.

### Phase 3: Validate execution, accounting and risk

**Estimate: 1–2 weeks**

**Tasks**

- Validate effective\-dated Indian charges and configurable slippage.
- Specify spread, participation, impact and latency assumptions appropriate to the data.
- Test gaps, ambiguous bars, unavailable liquidity and partial fills where applicable.
- Validate target, stop and time\-exit outcomes.
- Reconcile cash, reserved capital, positions and realised P&L.
- Test aggregate batch exposure, reduce\-only exits and duplicate prevention.
- Test restart recovery and persistent halts requiring explicit resume.
- Define counterfactual fills and exits using the same assumptions as executed trades.
- Run time\-separated historical evaluation and execution\-cost stress tests.

**Deliverables:** tested simulator, risk acceptance suite and baseline evaluation.

**Gate:** accounting reconciles, no known look\-ahead issue remains, and execution assumptions are conservative. If the strategy fails, revise or reject it before adding AI.

### Phase 4: Forward paper baseline and reporting

**Estimate: 1 week to configure; observation continues**

**Tasks**

- Schedule daily scans, paper entries, position monitoring and exits.
- Use existing operational interfaces before building a new dashboard.
- Add daily reconciliation, alerts and a decision trace.
- Report net expectancy, drawdown, turnover, exposure and benchmark\-relative results.
- Check dependence on the largest winning trades and specific periods/sectors.
- Separate trading P&L from all\-in operating\-cost P&L.
- Track simulated versus forward differences and operational failures.

**Deliverables:** stable daily paper loop and evaluation report.

**Gate:** operational stability and trading profitability are assessed separately. A fixed number of stable weeks does not establish durable alpha.

### Phase 5: AI research in shadow mode

**Estimate: 1–2 weeks**

**Tasks**

- Ingest official announcements and relevant news.
- Extract structured events with original source passages and timestamps.
- Connect one advisor model with explicit budgets and failure handling.
- Record hypothetical vetoes without changing baseline trades.
- Evaluate adverse\-event recall and extraction errors using human\-labelled examples.
- Record prompt, model, configuration, latency and cost.
- Audit discarded or supposedly irrelevant evidence.

**Deliverables:** source\-grounded research notes and shadow advisor records.

**Gate:** AI output is reliable enough to test. Persuasive explanations alone are insufficient.

### Phase 6: Controlled AI experiment

**Estimate: 1 week to configure; evidence collection continues**

| Book | Policy |
| --- | --- |
| A: Baseline | Deterministic strategy only |
| B: Event rules | Same strategy plus predefined deterministic event exclusions |
| C: AI veto | Same strategy plus the experimental AI filter |

**Tasks**

- Confirm or implement support for this comparison; it is an adaptation of the repository's discussed default experiment.
- Keep universe, signals, risk budget and execution assumptions comparable.
- Track a counterfactual trade for every rejected candidate.
- Measure avoided losses, missed winners, coverage, net expectancy and drawdown.
- Deduct advisor costs.
- Freeze rules during each evaluation window and version subsequent changes.
- Evaluate final selected trades, not just initial candidate quality.

**Deliverables:** matched\-book results and a retain/remove decision.

**Gate:** keep AI only if it improves a predefined objective after costs with adequate evidence. Higher win rate alone is not sufficient. If AI does not help, retain it only as a research assistant.

## 5\. Operational policies

- Pin and review dependency/repository upgrades before adoption.
- Keep configuration and strategy extensions separate where practical.
- Maintain one authoritative execution and accounting engine.
- Persist halts; restarting must not automatically resume trading.
- Exits must not accidentally create short positions.
- Missing required market data blocks new entries.
- In the optional\-advisor experiment, advisor failure falls back to the deterministic baseline only when required data and hard risk checks are valid; log the fallback explicitly.
- Research\-model failure must never disable protective exit logic.
- Backups must be restore\-tested.
- Preserve source inputs, decisions and version information for reproducibility.
- Stop adopting RakshaQuant if adaptation becomes more costly or less reliable than a small replacement.

## 6\. Deferred expansion

| Expansion | Prerequisite |
| --- | --- |
| Custom dashboard | Existing interfaces are demonstrably insufficient |
| Second strategy | First strategy has credible evaluation and stable operations |
| TradingAgents research components | Simpler advisor is insufficient and added value can be measured |
| Typed decision model or multi\-agent debate | A predefined incremental\-value experiment |
| Intraday research | Suitable data and a separate execution\-validation plan |
| Fundamental valuation module | Separate long\-horizon hypothesis and evaluation |
| Local models or Rust optimisation | Measured cost or performance bottleneck |
| Live broker pilot | Separate evidence, security, reconciliation, protective\-order and compliance gates |

## 7\. Immediate backlog

1. Freeze the first strategy and experiment specification.
2. Review and pin a RakshaQuant version.
3. Run tests/demo and inspect the core implementation.
4. Confirm swing\-trading extension points and overnight accounting.
5. Validate EOD data and configure the AI\-disabled baseline.

## Decision summary

RakshaQuant replaces planned custom paper\-trading infrastructure, conditionally on verification. We retain ownership of the strategy, trusted data, risk policy and proof of incremental AI value. Build the deterministic baseline first; add AI only as a controlled experiment; keep live trading separate.

