# Architecture decision records

These records capture the approved design choices for BharatQuant. They do
not certify that a feature is implemented or that an external claim is true;
current implementation status is in [DEVELOPMENT_STATUS.md](../../DEVELOPMENT_STATUS.md),
and unresolved external claims are listed in
[external-source-gates.md](./external-source-gates.md).

## ADR-001: Paper trading only

**Status:** Accepted.

**Decision:** The application will support research and paper accounts only.
It will not connect to a live broker or place real orders. `AUTO` may approve
eligible paper orders only after deterministic risk checks, strategy promotion,
reconciliation and kill controls pass. A model response cannot override them.

**Consequences:** Balances, fills and performance are simulations, not evidence
of real-world execution or returns. Never add broker credentials or live
execution as an implicit extension.

## ADR-002: Local modular monolith

**Status:** Accepted; foundation implemented.

**Decision:** Run the FastAPI backend and built React/TypeScript SPA locally on
loopback as one service. Add domain modules when features arrive; keep domain
policy independent from API, persistence and provider adapters. Use owned
interfaces only at real boundaries.

**Consequences:** The initial system avoids distributed-service complexity.
Later CPU-heavy work must be bounded and must not write paper-account state
outside the paper command handler. macOS-specific behavior belongs behind
platform adapters; unsupported platforms must report limitations explicitly.

## ADR-003: SQLite authority; files for immutable artifacts

**Status:** Accepted; SQLite migrations implemented.

**Decision:** SQLite is authoritative for transactional state, configuration,
jobs, reports, model-call records and paper-account transitions. Use ordered,
checksummed migrations. Add file-backed Parquet/Arrow for larger immutable
market datasets only when a concrete workload needs it; use DuckDB only for
appropriate analytical queries, not as a second transactional authority.

**Consequences:** Database and file writes need staged files, hashes, manifests
and reconciliation; they are not one atomic cross-filesystem transaction.
Backups and restores must verify integrity before replacing active data.

## ADR-004: Data provenance and verification gates

**Status:** Accepted; external verification is incomplete.

**Decision:** Do not seed market sessions, holidays, provider capabilities or
usage permissions from unverified claims. Verify current authoritative rules
and terms before enabling the affected feature. Preserve event, publication,
knowledge and receipt times separately; point-in-time reads require
`known_at <= as_of`. Uncovered dates and instruments fail closed only for
operations that depend on that coverage.

Delayed intraday data defaults to `WARN`, not `BLOCK`, but invalid or critically
stale data, an unusable price or an uncovered session still blocks new entries.
Show source age, quality and limitations.

**Consequences:** No market-data provider or session-dependent behavior is
enabled until its source, applicable terms and effective dates are reviewed.
See [external-source-gates.md](./external-source-gates.md).

## ADR-005: Task-class model routing

**Status:** Accepted; provider adapters are not enabled.

**Decision:** Classify model tasks server-side. Only `SMALL` tasks may use a
quality-gated local model; `REASONING` tasks require an eligible cloud model.
Provider capability, privacy, terms, quota and cost claims must be checked
before enabling an adapter. If confidence review is unavailable, deterministic
gates may permit a result explicitly labeled `RULES_ONLY`; a known veto remains
binding.

**Consequences:** Do not silently substitute a local model for cloud reasoning,
invent approval or report unknown usage as zero. Private account data is not
sent to a provider without a verified route and explicit privacy policy.

## ADR-006: Durable, idempotent schedules and replay

**Status:** Accepted; scheduler not implemented.

**Decision:** Persist schedule versions, job occurrences, attempts, leases and
results in SQLite. Give each occurrence an idempotency identity and recover
without duplicating logical work. Use explicit `RUN_ON_WAKE_WITHIN`, `SKIP` and
`ALWAYS_RUN` policies; never backdate a missed run. Seed editable report times
of 08:00 and 17:00 IST and a 07:45–17:30 keep-awake window, without promising
that the owner’s Mac will remain awake.

**Consequences:** Late, skipped and unavailable work must be visible. Replay
and retry must use recorded inputs and versions rather than mutate prior
results. The scheduler and its wake behavior still require target-Mac tests.

## ADR-007: Local owner security

**Status:** Partially implemented; private data routes do not exist.

**Decision:** Bind to loopback, validate Host and Origin, hash the owner
password with Argon2id, use host-only HttpOnly/SameSite cookies, protect
state-changing authentication requests with CSRF checks, rate-limit login,
serve the SPA with a nonce-based strict Content Security Policy, and record
security-relevant owner actions in a verified append-only audit chain. Every
future private route must independently enforce authentication and every
state-changing route must enforce origin and CSRF policy.

**Consequences:** Loopback HTTP is not TLS and browser cookies are not isolated
by port; other services on the same host are not a security boundary.
WebSocket origin/authentication controls are required if WebSockets are added.
The hash chain detects partial or accidental modification, not a fully rewritten
database without an independently retained trusted checkpoint. Password
recovery is not implemented.

## ADR-008: Third-party reuse and licensing

**Status:** Accepted; legal review remains open.

**Decision:** Prefer verified, maintained dependencies with compatible licenses,
provenance, supported platforms and required notices. Popularity is only one
signal. Write original application code and prompts; do not copy, vendor,
translate or reproduce third-party source or prompts. No-license material
requires permission before use.

**Consequences:** The dependency inventory is not a legal review or a
redistribution bundle. The project license and full redistribution notices
remain undecided.

## ADR-009: Deterministic news intake before any model involvement

**Status:** Implemented for ingest; no summarisation or model pass exists.

**Decision:** Ingest market news from Pulse by Zerodha's RSS feed and keep the
work deterministic. Never fetch article bodies: the feed already carries a title
and a one-line summary, which is the entire cost model. Deduplicate with a hash
of the normalised HTTPS URL plus an order-insensitive fingerprint of title
words, and classify with whole-word keyword rules. Store a separate, smaller
memory table holding only items that clear a structural-impact bar. Bound every
input: 2 MB feed, 500 items, 512/1,024-character fields, and clamp a feed's
future-dated publication times to ingest time rather than trusting them.

**Consequences:** Ingest is free and repeatable, but classification is only as
good as its keyword rules; a headline mentioning two topics is labelled by the
first matching category, and `OTHER` is a large bucket. Memory quality depends
on a hand-tuned impact rule, so the bar needs review against real feeds.
There is no summarisation, no article body, and no cross-source corroboration.
Any model-assisted enrichment must be opt-in and carry an explicit token
budget; it must not become a per-article call on ingestion.

## ADR-010: Runtime path must be importable without development tooling

**Status:** Accepted.

**Decision:** Every module the CLI imports at startup must be satisfiable by
runtime dependencies alone. `httpx` is a runtime dependency because the news
agent's fetcher imports it. Verification is a clean virtualenv plus
`pip install .` and `python -c "import bq.cli"`, not a check inside the
development environment where dev extras are already present.

**Consequences:** Adding a new runtime import path requires checking both the
locked production dependency set and the dependency notices. A test that only
runs in the development environment cannot detect this class of packaging
failure; the install check must be performed explicitly.

## ADR-011: Agent tooling adapted from Agent Zero, reimplemented not copied

**Status:** Accepted.

**Context:** We are about to build several agents (news, market research,
fundamentals). Agent Zero (`agent0ai/agent-zero`) is a mature reference for a
tool-based agent loop: a `Tool` base that returns a message plus a `break_loop`
flag, tool policy kept separate from tool discovery, a `response` terminator, an
`unknown` fallback, and a compressible conversation history. Reading it is
useful; copying it is not. The project plan says to prefer vetted dependencies
and original code/prompts, and not to copy external source or prompts.

**Decision:** Reuse the *structure* only, reimplemented in original code:

- A `Tool` base returning `(message, break_loop)` and a declared argument
  surface, with bounded validation (`bq/tools/base.py`).
- Explicit `ToolRegistry` registration plus `AgentToolPolicy`/`ToolRunner`,
  so mutation can be denied per agent profile (`bq/tools/registry.py`). This is
  what lets a research agent hold `news_ingest` while a read-only analyst
  profile cannot.
- A strict tool-call parser (`bq/tools/parsing.py`) — stricter than upstream,
  which repairs malformed calls. We fail loudly instead.
- A deterministic loop with bounded history and a rolling summary
  (`bq/agent_runtime.py`) that calls no provider.

We deliberately do **not** take: import-tool-classes-by-filesystem-path (an
execution surface), the streaming/extension hook system, LiteLLM coupling, the
plugin marketplace, the WebSocket/UI chat layer, or the vector-DB memory. Those
solve Agent Zero's product problems, not ours.

**Consequences:** Agents get a uniform, safe tool surface and a policy boundary
we control. If we later want MCP tools or sub-agents, they plug into the same
`ToolRegistry` without changing the loop. The cost is that we own the
implementations and must test them ourselves (done in `tests/test_tools.py`);
there is no upstream to pull fixes from, which is acceptable because the
surface is small and fully covered.

## ADR-012: LLM-calling helpers adapted from Agent Zero (MIT)

**Status:** Accepted.

**Context:** We now make real model calls. Agent Zero has a mature LLM layer.
Its `helpers/litellm_transport.py` (2,297 lines) is bound to LiteLLM and the
OpenAI Responses API, and `helpers/call_llm.py` to LangChain — neither
dependency is one we use, so neither is portable. The question was what can be
reused *legitimately and usefully* without dragging in that stack.

**Decision:** Reuse the self-contained, transport-independent pieces, and keep
the vendored file faithful.

- **Adapted (original reimplementation, structure informed by upstream):**
  - `bq/llm_result.py` — `LLMResult`/`LLMUsage`/`FunctionCall` plus
    `parse_usage` for both wire shapes. Upstream's OpenAI-Responses state
    (response ids, input/output items) is omitted because we don't use it.
  - `bq/llm_extract.py` — `split_thinking` (separate `<think>`/`<reasoning>`
    blocks from visible text) and tool-call normalisation for OpenAI and Gemini
    shapes, plus finish-reason mapping.
  - `bq/llm_retry.py` — transient-vs-permanent error classification for
    `httpx` errors, capped exponential backoff, and a sliding-window
    `RateLimiter` (ported from upstream's `rate_limiter.py`).
  - `bq/llm_tokens.py` — token estimation and trimming. Upstream uses
    `tiktoken`; we use a documented chars-per-token estimate and **do not**
    add the multi-megabyte tokenizer, because this is for budgeting and
    trimming, never for billed usage.
- **Vendored verbatim:** `bq/dirty_json.py` from `helpers/dirty_json.py` (MIT).
  It is a mature recursive-descent parser for near-miss JSON; rewriting it only
  adds bugs. It is excluded from ruff/mypy so it stays a faithful copy, and is
  used **only** via `parse_tool_call(recover=True)` — strict parsing remains the
  default so a malformed tool call cannot silently become a different call.
- **Not ported:** LiteLLM transport, Responses-API state machine, LangChain
  `call_llm`, provider YAML registry, model preset system, `tiktoken`. Each is
  tied to a dependency or product surface we don't have.

**Consequences:** We get robust reasoning/tool-call parsing and retry/rate-limit
behaviour without a new runtime dependency (the guard test still passes). The
cost is that we own these modules and must maintain them; they are covered by
`tests/test_llm_ported.py`. Attribution is recorded in each file header and in
`THIRD_PARTY_NOTICES.md`. If we later adopt LiteLLM, the transport could be
revisited, but the current direct-`httpx` clients are simpler and auditable.

## ADR-013: Kite Connect is a read-only market-data vendor

**Status:** Accepted. Supersedes no earlier record; this ADR **reaffirms
ADR-001** and draws the boundary that a proposed change wanted to cross.

**Context:** A request to port TradingAgents' multi-agent analysis would also
have added a Zerodha Kite Connect **execution** layer: sandbox order routing, a
live-approval mode and a `LIVE_AUTO` mode. ADR-001 says this application "will
not connect to a live broker or place real orders … Never add broker credentials
or live execution as an implicit extension", and plan §2.2 lists live order
placement as a non-goal. Adding an executor would therefore have overridden an
accepted decision silently.

Kite is nevertheless a genuinely better **market data** source than Yahoo for
Indian equities: it is the broker's own API, it serves real-time quotes and
intraday candles, and it publishes an instrument master that maps
`exchange:tradingsymbol` to a stable numeric token.

**Decision:** Kite Connect is a **read-only data vendor**.

- Allowed calls are limited to `kite.quote`, `kite.ohlc`,
  `kite.historical_data` and `kite.instruments`.
- `bq/data/vendors/kite_vendor.py` must contain no `place_order`,
  `modify_order`, `cancel_order` or autoslice call. A test asserts the module
  exposes no order-placing method, so a future edit cannot quietly add one.
- Kite is disabled by default (`kite_enabled = false`). With it disabled the
  vendor router has no Kite vendor and reads fall through to Yahoo; there is no
  failure path and no warning noise.
- Kite credentials live in their own `broker_credentials` table, separate from
  `provider_api_keys`, because Kite is not an LLM provider and must not appear
  in the provider list. They follow the existing masked-secret handling: never
  returned in plaintext, only a masked hint, with an audit event on change.
- An expired Kite daily access token raises `VendorUnavailableError`, so
  read-only analysis degrades to Yahoo rather than failing.

**Consequence — TradingAgents reuse stays R3.** Because there is no execution
layer to port into, and because plan §4.3.1 already scopes TradingAgents as
"prompts and design reference", the migration is a **reimplementation**, not a
vendoring: the analyst/debate/judge/risk-team structure, the per-domain vendor
chain with typed failures, and the look-ahead test discipline are studied and
rewritten in original code, exactly as ADR-011 and ADR-012 did for Agent Zero.
`docs/migration-tradingagents.md` records the component map.

**Consequences:** A useful real-time Indian data source is available without
weakening the paper-only guarantee. A future live-execution phase must supersede
ADR-001 explicitly and complete plan §25's SEBI, static-IP and external-source
gates first; it cannot arrive as a quiet extension of this vendor.

## ADR-014: Deterministic indicators in pandas and numpy

**Status:** Accepted.

**Context:** The technical analyst needs RSI, MACD, Bollinger Bands, EMA/SMA,
ATR, VWAP, ADX, drawdowns and swing-structure support/resistance over a few
hundred to a few thousand bars. The commissioning prompt required these be
computed deterministically in code rather than by a model, which this project
already requires in principle (plan §3, principle 11). The repository until now
had **no** numeric stack: no pandas, numpy, polars or TA-Lib, and
`tests/test_runtime_dependencies.py` fails the build if a module reachable from
`bq.cli` imports an undeclared package.

**Decision:** Add **pandas** and **numpy** as declared runtime dependencies and
implement the indicators as pure functions in `bq/compute/indicators.py`.

- Champagne problem considered and rejected: hand-rolling Wilder smoothing and
  rolling-window statistics in stdlib Python is more code to get subtly wrong
  (RSI seeding, ATR gap handling, MACD warm-up) than it saves.
- Polars was not chosen: plan §6.1 prefers it for large-frame compute, but this
  workload is a few thousand rows and pandas keeps the ported conventions
  readable.
- TA-Lib was not chosen: it needs a C toolchain on the target Mac, and plan §4.2
  schedules it as a later dev-extra cross-check rather than a runtime need.
- Prices remain `float64` inside indicator code, because that is what pandas
  produces. The single conversion point back to `CurrencyPaise` — used for
  sizing and the trade plan — is explicit and tested, so exact-paise accounting
  downstream is unaffected.
- Both packages are added to `THIRD_PARTY_NOTICES.md` (BSD-3-Clause each) and the
  dependency guard must pass before any indicator code lands.

**Consequences:** The numeric stack becomes part of the runtime install (pandas
pulls in `python-dateutil`, `pytz` and `tzdata`), which is the real cost
accepted here. `ADR-010`'s discipline still applies: the guard test decides
whether a declaration is correct, and the notices file must stay in step. If the
numeric workload later grows into frame-scale analytics, plan §6.1's Polars path
remains open without changing the indicator module's public functions.

## ADR-015: Delayed data is usable for paper execution, blocking only when stale

**Status:** Accepted. Elaborates ADR-004 for the execution path.

**Context:** ADR-004 already says delayed intraday data defaults to `WARN`, not
`BLOCK`, and that critically stale data blocks new entries. The execution layer
had to turn that policy into one boolean — `evaluate_pretrade`'s `data_usable` —
and the obvious question was whether a `delayed_yfinance` price should set it
false.

For **paper** execution it should not. A paper fill is a simulation: a delayed
price is a legitimate input, and refusing every plan that came from Yahoo would
make the paper engine unusable on a free data source, which is the whole point of
the default configuration. The real risk that matters for a paper run is *stale*
data — a price from days ago — not a price from fifteen minutes ago.

**Decision:** `bq/execution/paper_executor.py` maps freshness as follows.

| Plan label | Within its freshness budget | `data_usable` | Reported as |
|---|---|---|---|
| `realtime_kite` | yes | `True` | `realtime` |
| `delayed_yfinance` | yes | `True` | `delayed_warn` |
| either | no | `False` | `stale_block` |

**The two timeframes use different clocks, deliberately.**

- **Intraday** is measured in wall-clock seconds (15 minutes). A 15-minute candle
  really does lose value in 15 minutes.
- **Swing** is measured in **weekday sessions missed** (budget: 2), and age starts
  from the bar's **close**, not its open — a daily bar carries the session open
  stamp but is not settled until the close.

A calendar-seconds threshold is actively wrong for daily bars: it blocks every
Monday morning for a plan built on Friday's close, even though no session has
traded in between and Friday is the freshest data that can exist. Live testing
found exactly that false block. Public holidays are not modelled yet, so a holiday
week counts one extra missed session — erring toward blocking, which is the safe
direction, with the existing trading calendar as the future fix.

All thresholds are **unvalidated starting points**, recorded as such rather than
presented as a verified rule.

**Consequences:** The paper engine works on free delayed data while still
refusing history that is too old to be meaningful, and every run records which
case applied. This mapping is **specifically for paper** and must not be copied
into a live path: for a live order, delayed data has to be a hard block. Because
ADR-001 forbids live execution entirely, that is a future decision, not a flag to
flip — and this ADR exists so the difference is visible rather than implicit.

## ADR-016: Config-driven three-tier model routing

**Status:** Accepted; Phase 2 implements the router.

**Context:** Every agent needs a model choice, but the application had exactly
one global provider and model name in `app_settings`. The commissioning request
asked for three tiers — a calibrated decision model, a strong reasoning model and
a fast worker — switchable from configuration with no code edits. It also asked
for a YAML settings file and `pydantic-settings`. ADR-012 had already recorded a
deliberate decision not to port Agent Zero's provider YAML registry or model
preset system, and `tests/test_runtime_dependencies.py` fails the build if a
CLI-reachable module imports an undeclared package.

**Decision:** Configuration remains in SQLite `app_settings`, edited through the
owner-only Settings UI and API. No YAML loader and no `pydantic-settings`.

- The three tiers are `decision`, `reasoning`, `flash`, represented by a new
  `TierName` enum. **`ModelTier` (`LOCAL`/`FREE`/`PAID`) is not extended** — it
  belongs to the eligibility gate and to canonical `task_definitions` JSON, and
  new members would invalidate stored definitions.
- Per-agent assignment lives in one canonical JSON setting, `agent_routing`.
  Any unknown agent id resolves to `reasoning` and is logged.
- Tier models are configured under `tier.<name>.*` keys. When those keys are
  absent, reasoning and flash derive from the legacy `provider`/`model_name`
  pair, so an existing install behaves exactly as before.
- A tier is configured only when it has a model and a matching provider key.
  `model_route_configured` keeps its legacy meaning for `/api/v1/llm/complete`.
- `ModelRouter` still runs the existing `select_model` gate for every call; the
  configuration satisfies the gate rather than bypassing it.

**Consequences:** Swapping a model is a settings change. The cost is one more
settings surface and a migration to admit `TYPESAFE_JEV` to
`provider_api_keys`; the settings table itself needs no schema change. The YAML
question is closed for this project unless a later requirement justifies a
second configuration source.

## ADR-017: Jev is a preferred decision provider, never a dependency

**Status:** Accepted; Phase 2 implements the client and ladder.

**Context:** TypeSafe Jev is a System One model that answers typed Choice,
Score and Noul questions with calibrated values faster and cheaper than a text
model. The commissioning request named it the default decision tier. But an
external endpoint can be absent, unconfigured, rate-limited, changed or subject
to terms that have not been reviewed. Making the analysis pipeline depend on it
would turn an optional accelerator into a single point of failure.

**Decision:** Decision questions use Jev when it is configured and reachable,
and otherwise run on the configured Flash tier, then Reasoning, as JSON that is
validated against a schema generated from the same question set. If no model
tier is configured, the decision node records `rules_only` and the deterministic
pipeline continues.

- `declared` routes `openrouter_jev` and `requesty_jev` are recognised but
  rejected at save time until their endpoints and terms are verified.
- Every answer carries `decision_source` (`jev_native`, `llm_fallback`,
  `rules_only`) and `fallback_used`/`fallback_from` in the run ledger.
- Jev receives only pre-computed facts; it never performs arithmetic.
  Dependent questions are separate coordinator steps because one Jev request
  evaluates its questions in parallel and in isolation.
- Brier calibration is bucketed by `decision_source`, so fallback performance is
  measured separately and never mistaken for Jev's calibration.
- The Jev API key follows the existing masked-secret rules. The endpoint and
  request/response shapes are recorded in `external-source-gates.md`; terms and
  privacy review remain open, so live traffic stays owner-key opt-in.

**Consequences:** A missing or broken Jev key degrades quality, not availability.
The fallback answers are less calibrated than native Jev by design, which is why
they are labelled and scored separately. ADR-001 and ADR-013 are untouched: this
ADR concerns model routing only, not broker execution.

## ADR-018: Jev judgments are a battery; policy, risk and calibration stay in code

**Status:** Accepted; Phase 2 implements the state, battery and client, Phase 3
the calibration store.

**Context:** Studying a production Jev trading system
(`buberlo/jev-trader`, MIT, surveyed 2026-11-26) made the correct boundary
explicit. That system does not ask a model to run the strategy. A deterministic
feature engine builds a dense numerical state (under 400 tokens), one Jev call
answers six atomic judgments (two Choice, two Noul, two Score), a code policy
maps those judgments to an action, a hard risk engine can veto, and every
`(state, decision, outcome)` triple is logged for calibration. The model supplies
judgments; code supplies thresholds, execution and risk.

**Decision:** BharatQuant adopts the same separation, with original code:

- `bq/decision/state.py` builds a `DecisionState` from already-computed facts.
  Floats are rounded, `None` values are dropped, prose is never included, and
  the serialized state is bounded (16 KiB / ~2,000 tokens).
- `bq/decision/battery.py` defines atomic question sets per node (screener,
  news triage, evidence, final gate). One request carries only independent
  questions; dependent judgments are separate coordinator steps.
- `bq/decision/client.py` pins the model version, records the returned model,
  request id and latency, warns on a version mismatch, and sets the HTTP timeout
  to 80 % of the decision budget. It does not retry past the deadline.
- `bq/decision/fallback.py` provides a deterministic `HeuristicJudge` with fixed
  0.5 confidence for the case where no model answers. It is reproducible from
  the state alone; it never claims calibration.
- `bq/decision/policy.py` owns every threshold and size multiplier. No prompt or
  model output may set a threshold.
- `bq/decision/calibration.py` records decision triples and computes
  Brier/log-loss/ECE/reliability/Platt **per `decision_source` and per returned
  model version**, so a fallback or a silent model upgrade cannot be mistaken
  for native-Jev calibration.
- The existing hard risk gate stays the last word. Phase 4 maps decision
  provenance to a deterministic size scale (`jev_native` 1.0,
  `llm_fallback` 0.75, `heuristic` 0.5, `rules_only` no new entry) and adds a
  latency breach hold.

We deliberately do **not** reuse: the async SDK (we keep injectable `httpx` and
no new runtime dependency), Avellaneda–Stoikov pricing, the crypto venue loop,
`pydantic-settings`, or fractional Kelly. No source is copied; only the
architecture is reused, consistent with ADR-008 and ADR-011.

**Consequences:** Jev is fast and cheap because it only answers typed questions,
and a missing key degrades through Flash/Reasoning to the heuristic without
losing the run. The cost is more modules and a calibration store, but each has a
single responsibility and can be tested without a network or a model. Thresholds
are reviewable in code/config rather than buried in prompts, and fallback quality
is measured instead of assumed.

## ADR-019: Prompts are versioned configuration; contracts and thresholds are code

**Status:** Accepted; Agent Lab implements it.

**Context:** The owner needs to refine agent wording without editing code, but the
analysis agents depend on fixed JSON shapes and on deterministic numbers. A free
text editor that can silently remove a placeholder would turn a prompt
refinement into a runtime `KeyError`; letting a prompt change thresholds or the
decision question set would move policy out of code. The Agent Lab also needs to
be useful before Phase 3 adds the specialist agents.

**Decision:** Prompts are editable configuration, with these boundaries.

- Defaults live in `bq/agent_prompts.py` (imported from
  `bq/analysis/prompts.py`), so the code remains the baseline.
- Every save appends a version to `agent_prompts` (migration `027`). Rows are
  immutable and cannot be deleted; "reset" writes the default text as a new
  version. The save is audited with a content hash, never the prompt text.
- A template must keep every required placeholder for its agent and must not
  introduce unknown fields; it is format-checked with sample values before it is
  stored. This is what keeps a refinement from breaking a live run.
- `Debate`, `RiskAnalyst` and `ModelRouter` accept a `PromptSource`; the API and
  CLI pass `PromptStore`. A missing or broken store falls back to the code
  default rather than failing a run.
- Editable today: bull, bear, research judge, risk analyst, and the
  decision-screener LLM fallback system prompt. **Not editable:** the decision
  question names, option labels, score levels, answer types, policy thresholds,
  size scales and risk limits. Jev question text remains a code contract.
- The new Agent Lab tab exposes both the decision preview and the prompt editor.
  `POST /api/v1/decision/preview` is owner-only, origin- and CSRF-checked and
  never places an order.

**Consequences:** The owner can test the decision layer and refine wording from
the browser, and every refinement is reviewable and reversible. The cost is a
small store and injection surface; because defaults are code and the store is
append-only, a bad prompt cannot erase the baseline. Phase 3 agents will add
their prompt definitions to the same registry instead of inventing another
configuration path.

## ADR-020: The Super Agent ranks; the coordinator gates and never delegates a BUY

**Status:** Accepted; Strategy desk implements it.

**Context:** The owner wants to supply a strategy and have the Super Agent use
the specialist agents. Letting a model choose the universe, invent a trade the
deterministic pipeline rejected, or resize a plan would move policy into a
prompt. Running one broken symbol must also not destroy the whole run.

**Decision:** The coordinator is code (`bq/super_agent.py`); the Super Agent is a
bounded synthesis step.

- The coordinator resolves and caps the universe, runs the existing per-symbol
  pipeline, and records a per-symbol error instead of aborting the run.
- Only a deterministic BUY plan is a candidate. The coordinator accepts plans in
  composite/confidence order until `max_positions` or available cash is reached;
  it never changes the sizer's quantity. A plan that does not fit is rejected
  with a reason.
- The reasoning tier receives the strategy, constraints and one compact payload
  per candidate. It may rank, downgrade to HOLD/AVOID, and comment; it may not
  create a BUY, invent a symbol or alter a number. A rank for an unknown symbol
  is ignored; a BUY for a non-candidate is impossible because only accepted plans
  are rendered.
- With no reasoning answer the run falls back to a rules ranking by composite and
  confidence, labelled `source="rules"` / `fallback_from="reasoning"`.
- The Super Agent prompt (`super_agent_cio`) is editable in Agent Lab with
  required placeholders validated before save. Thresholds, sizing and risk stay
  in code.
- Accepted plans are research output. The endpoint does not touch the broker and
  does not create paper orders; a future execution step must go through the
  existing risk gate and paper executor.

**Consequences:** A strategy can be explored end to end from the browser without
letting a model override risk or manufacturing a trade. The cost is a sequential
per-symbol run (bounded to eight) and a second reasoning call per strategy. The
per-symbol traces and route ledger make it clear which agents actually
contributed and which model answered.

## ADR-021: Stage 1 specialists are optional, point-in-time briefs

**Status:** Accepted; Phase 3A implements news, regime and corporate events.

**Context:** The debate was reading technical facts only. News existed in a
deterministic store, index data was available through the existing vendor
router, and corporate headlines were already classified, but none reached the
analysis pipeline. Wiring them as mandatory nodes would let a missing feed or an
index outage kill a run that could still produce a valid technical plan.

**Decision:** Stage 1 specialists emit a typed `SpecialistBrief` and are optional
at the DAG level.

- News context reads `NewsStore.recent(as_of=...)` and active long-term memories.
  `recent` now filters both `published_at` and `ingested_at` against `as_of`, so a
  backtest cannot see a later headline. It matches the symbol's trading name and
  never fetches an article body.
- Market regime reads NIFTY 50, NIFTY Bank and India VIX through
  `DataDomain.INDICES` and classifies the regime from code-computed SMA, ADX, ATR,
  returns and VIX values. The model receives facts, never a request to label the
  regime.
- Corporate events extracts symbol-specific corporate/event headlines from the
  same news store. This is explicitly a placeholder for a verified fundamentals
  vendor; it does not invent dates, valuations or estimates.
- A specialist failure records a `degraded` trace and contributes no facts. The
  run continues. At most 18 specialist facts are appended to the debate and risk
  prompts, alongside the technical facts.
- `AnalysisResult.specialists` and the Technical tab expose what each specialist
  contributed. Empty or unavailable briefs remain visible as degraded rather
  than disappearing.

**Consequences:** The debate now sees news, regime and event risk without a new
data dependency or a new model call for the deterministic parts. The cost is a
wider prompt (bounded) and a shallow fundamentals specialist until a verified
vendor is reviewed. The next memory slice can consume the same briefs for
episodic and semantic storage.

## ADR-022: The final gate can only block execution

**Status:** Accepted; Phase 3B implements it.

**Context:** The deterministic plan is the only source of prices and quantity.
A final model review is useful, but if it could create a trade or change the
plan it would move policy back into a prompt. The gate also needs to see the
plan, not just the technical report, or it cannot judge execution.

**Decision:** `bq/analysis/final_gate.py` runs the `final_gate` battery after the
deterministic decision node.

- `DecisionState` now carries the plan's action, entry, stop, target, reward-risk,
  quantity and notional alongside the technical state.
- The gate uses the same Jev → Flash → Reasoning → heuristic ladder and the
  code-owned `DecisionPolicy`. It answers action, execute probability and
  conviction tier.
- A gate result can only block: when it does not execute, the pipeline adds a
  `final_gate:` veto and leaves the original plan intact for inspection. It
  cannot create a BUY, change a stop or quantity, or alter the sizer output.
- `AnalysisResult.final_gate`, the trace and the Technical tab expose the gate
  source, model, action, reason and probability. The Strategy desk excludes a
  plan the gate blocked.
- No order is placed by this node.

**Consequences:** The final review becomes visible and auditable without
weakening the sizer/risk ownership. The pipeline has one more optional node; a
gate failure degrades to a trace and leaves the deterministic plan unchanged.

## ADR-023: Episodic memory is immutable decisions plus append-only outcomes

**Status:** Accepted; Phase 3B implements the store and shadow recording.

**Context:** Calibration is only trustworthy if a later outcome cannot rewrite
what the system believed at decision time. Shadow trades are also the only way
to learn from skipped setups, not just executed ones.

**Decision:** Migration `028` stores two append-only tables.

- `episodic_memory` records the decision: symbol, as_of, recorded_at, source,
  decision source, model, regime, action, status, plan prices/quantity,
  predicted probability and bounded JSON detail. It is immutable by trigger.
- `episode_outcomes` records one resolution per episode with `resolved_at`,
  binary outcome, realized R, close price and source. It is append-only and
  cannot be replaced.
- The store records shadow episodes from analysis and strategy runs. A failed
  memory write never breaks an analysis response.
- `calibration(as_of=...)` joins episodes and outcomes, filters **both**
  `episode.as_of` and `outcome.resolved_at` against the requested instant, and
  buckets Brier/log-loss/ECE by `decision_source` and `model`. Unresolved
  episodes are excluded; no predicted probability means no Brier sample.
- The calibration view is owner-only and no-store; the Today panel displays it.

**Consequences:** Jev-native, fallback and heuristic performance are measured
separately and cannot leak future outcomes into historical evaluation. The
scheduled resolver is still pending, so the buckets remain empty until outcomes
are resolved; the store API is ready for that job.

## ADR-024: Semantic memory is immutable documents plus a rebuildable index

**Status:** Accepted; Phase 3C implements it.

**Context:** Research documents need point-in-time retrieval, but the SQLite
build on every target machine may not include FTS5. Search must also never see a
document published, known or recorded after the requested `as_of`.

**Decision:** Migration `029` stores immutable `semantic_docs` and a
deterministic `semantic_terms` index. FTS5 is a derived cache, not authority.

- `SemanticMemoryStore` creates `semantic_docs_fts` lazily only when FTS5 is
  available, backfilling existing documents on creation. If creation fails, the
  store uses `semantic_terms` and returns the same `SemanticHit` contract.
- Search filters `published_at`, `known_at` and `recorded_at` against `as_of`,
  optionally by symbol, and caps query/limit sizes.
- Documents and terms are immutable by trigger. Source URLs must be HTTPS
  without embedded credentials; metadata is bounded canonical JSON.
- The owner-only no-store search route and the Agent Lab panel expose the result.
  Indexing news headlines into semantic memory is a later ingestion hook.

**Consequences:** Search works on a plain SQLite build, and FTS5 is an
accelerator that can be rebuilt from the authoritative tables. A later resolver
or ingestion job must call the store to add documents; the search surface is
ready.

## ADR-025: Evidence verification starts as a deterministic citation check

**Status:** Accepted; Phase 3C implements it.

**Context:** Specialist claims cite news ids. Before a debate consumes them, the
system should know whether those ids were actually retrievable at `as_of`.
Asking Jev to entail each claim is useful but adds a model call and calibration
burden; the cheap, decisive check is whether the cited evidence exists.

**Decision:** `bq/analysis/evidence.py` checks each claim's `evidence_ids`
against the point-in-time news store before bull/bear.

- A claim with no evidence ids is unverified. A claim is verified only when every
  cited id exists at `as_of`.
- The pipeline replaces the specialist briefs with claims carrying
  `verified=True/False`, records a trace (`verified` / `unverified` counts), and
  exposes missing ids. The Technical tab shows the claim status.
- Verification is optional: a missing store or failure records a degraded node
  and leaves the briefs untouched.
- This is a citation check, not an entailment proof. The Jev Noul pass remains a
  later refinement; it must not let a model invent or remove evidence ids.

**Consequences:** Claims that cite nothing or cite a story the run could not
know are visible before the debate, at zero token cost. The limitation is
explicit: a real story can still be misread, which is what the future model
verification is for.

## ADR-026: Outcomes are resolved by deterministic first-touch, never rewritten

**Status:** Accepted; Phase 3D implements it.

**Context:** Brier calibration is meaningless until a shadow decision has an
outcome. The outcome must be computed from data that was not available at
decision time, but the original episode must remain exactly what the system
believed. A model must not decide whether its own trade worked.

**Decision:** `bq/memory/resolver.py` resolves due episodes in code.

- An episode is due when `as_of + horizon` has passed. The default horizon is
  five calendar days; both horizon and batch size are bounded parameters.
- Only daily bars **after** the episode's `as_of` and at or before the horizon are
  considered. One bad symbol produces a report note; the batch continues.
- A BUY plan resolves on stop/target first-touch. If a bar touches both, the stop
  wins (conservative). If neither is touched, the horizon close decides the
  binary outcome and realized R. A no-plan HOLD/SELL/AVOID episode resolves on
  the forward return from the stored `last_close`.
- Resolution appends one outcome with the bar's timestamp, outcome, realized R,
  close price and `outcome_resolver` source. The episode is never updated.
- The owner-only, CSRF-protected resolve route and the calibration panel button
  call the resolver explicitly. Wiring it to a registered schedule is pending.

**Consequences:** The calibration loop is testable and reviewable end to end
without a model in the outcome path. The five-day horizon and first-touch rules
are deliberate starting points, not validated strategy claims; a later research
slice can compare horizons against recorded outcomes. Calendar days are used,
so exchange holidays are not yet modelled.

## ADR-027: Procedural playbooks are guidance, not policy

**Status:** Accepted; Phase 3E implements it.

**Context:** The owner wants to select and refine strategy guidance without
editing code, but a strategy playbook must not become an alternative risk policy
or a place to hide thresholds. The prompt editor already established the pattern:
append-only versions, code-owned defaults, bounded text.

**Decision:** Migration `030` adds append-only `procedural_playbooks` and
`bq/memory/procedural.py`.

- Three defaults are code-owned: momentum continuation, capital preservation and
  event-aware. Name, description and the playbook set are code-owned; the owner
  edits only the bounded content.
- Saves append a version; reset writes the default as a new version. Content is
  length- and control-character checked; audit rows carry a hash, not the text.
- The Strategy request may select a playbook. The Super Agent resolves it before
  any symbol runs and includes a bounded `{id, name, version, guidance}` block in
  its reasoning constraints. `StrategySpec.playbook` records the choice.
- A playbook cannot change the decision ladder, thresholds, size scales, cash or
  position gates, or risk vetoes. It shapes ranking commentary and emphasis only.
- The Agent Lab editor and Strategy selector are owner-only and no-store.

**Consequences:** Strategy guidance is editable and reviewable without moving
policy out of code. An unknown playbook fails before symbol work; a missing
playbook is allowed. The next research step is to compare playbook-selected runs
against outcomes, which the episodic memory already records.

## ADR-028: The evidence Noul pass may reject but never rescue a claim

**Status:** Accepted; Phase 3F implements it.

**Context:** The deterministic verifier proves that a cited news id existed at
`as_of`; it does not prove the story supports the claim. A typed Noul judgment is
a cheap way to add that second check, but it must not let a model invent evidence
or override a missing citation.

**Decision:** The evidence verifier gains an optional Noul pass over the same
cited claims.

- `ModelRouter.decide` accepts a bounded mapping as well as a `DecisionState`, so
  the evidence battery can use a claim-focused state (claim text plus cited
  titles/summaries) through the ordinary Jev → Flash → Reasoning ladder.
- The citation pass runs first. Claims with no ids or any missing id are marked
  unverified and are never sent to a model.
- Eligible claims are sent in one battery request, one Noul each, up to 20. A
  claim passes when the Noul probability is at least 0.60.
- The model can only lower a claim from verified to unverified. It cannot add
  evidence ids, add claims or turn a missing citation into a pass.
- No router, no configured model, or any model failure falls back to the
  citation result and labels it `decision_source="citation"`. A model pass
  records the source and model in `EvidenceResult`, the run payload and the UI.
- `HeuristicJudge` deliberately refuses the evidence battery, so a no-model run
  is a citation check, not a fabricated judgment.

**Consequences:** Evidence quality is checked at two levels without making the
pipeline depend on a model. The Noul threshold is a code-owned starting point
like the other decision policy values; it can be calibrated from recorded
outcomes later.

## ADR-029: The execution seam is paper-only and the gate reads one snapshot

**Status:** Accepted; Phase 4A implements it.

**Context:** The paper executor already applied `evaluate_pretrade`, but the owner
could only reach it through the CLI with hand-supplied account figures. The
Strategy desk produced accepted plans that could not be taken any further, and
the Trade nav item was inert. A UI seam is useful only if it cannot invent
account state, risk limits or a session state.

**Decision:** Phase 4A adds a deterministic account snapshot and a typed adapter,
not a broker integration.

- `build_execution_context` reads cash, positions, average-cost matched fills, order rate
  and account status from one SQLite read transaction. Cash is the paper-account
  balance; symbol exposure is marked at the proposed entry price; loss limits use
  **realised** P&L only. Unrealised P&L is not stored, so the UI says so.
- Session state comes from the reviewed exchange calendar. Missing coverage fails
  closed; an owner-supplied override is returned with `session_source =
  owner_override`, never hidden.
- `ExecutionMode` has `PAPER`, `KITE_SANDBOX` and `LIVE_APPROVAL`. Only PAPER
  resolves to an adapter. The other modes raise `ExecutionDisabled`; no broker
  object is constructed and no Kite order method is added.
- `PaperExecutor.preview()` runs the gate without placing. `execute()` calls it,
  enforces the code-owned 1.5 R:R floor, preserves `suggested_quantity` and replays
  a duplicate `client_order_id` instead of creating a second order.
- Every risk limit is explicit on the request. The Trade tab requires all six; it
  does not prefill invented values. Placing is enabled only for an ALLOW preview.
- The Strategy tab may stage an accepted plan into Trade. Staging copies the
  validated plan; the execution gate still runs separately and can block it.

**Consequences:** Paper orders are reachable from the UI and remain PENDING after
placement. Filling, partial fills and reconciliation are the Phase 4B slice. No
ADR-001 or ADR-013 protection is weakened: the only enabled adapter is paper.

## ADR-030: Paper fills are owner-triggered and atomic

**Status:** Accepted; Phase 4B implements it.

**Context:** Phase 4A places a PENDING paper order. A pending order that can never
fill is not a useful paper lifecycle, but adding a matching engine or a market
order would invent behaviour the system does not model. The existing
`PaperTradingStore.fill` already performs the required transaction; it needed an
authenticated API and a UI action.

**Decision:** Fills remain manual and deterministic.

- The owner fills an order through `POST /api/v1/paper/orders/{id}/fill`. The
  request supplies quantity, an exact price in paise and an idempotency key.
- The store enforces the order limit: a BUY cannot fill above its limit (a SELL
  cannot fill below it). Quantity cannot exceed the remaining quantity and must be
  positive.
- The fill is one SQLite transaction: insert the fill, update the order status and
  filled quantity, update the position and append the exact-paise ledger entry. An
  insufficient cash balance or a short position rolls the whole transaction back.
- `(order_id, idempotency_key)` identifies a fill. Repeating the same key returns
  the original fill and updated order; it does not fill twice.
- The Trade tab fills the remaining quantity at the order limit by default, or at
  an explicit lower BUY price. Partial quantities are supported by the API.
- `GET /api/v1/paper/accounts/{id}/reconciliation` is read-only and checks order
  fill totals, fill totals, ledger totals and open positions.
- There is still no market order, no automatic matching, no short position and no
  broker adapter. ADR-001 and ADR-013 stand.

**Consequences:** The paper loop now reaches cash, position and ledger changes
from the UI, and a retry cannot double-fill. Cancellation and an exit/sell flow
remain deferred; a filled paper position is currently exited only by a manually
created SELL order through the store, not through the execution adapter. The
reconciliation endpoint is a consistency check, not a broker reconciliation.

## ADR-031: Acceptance drives the HTTP surface with a deterministic vendor

**Status:** Accepted; Phase 5 implements it.

**Context:** Unit tests cover each module and each API route, but nothing exercised
the whole chain from batch analysis through Strategy, execution, fill and
reconciliation. A live three-symbol run is useful but not reproducible in CI: it
depends on the network, the data vendor and the wall clock. The acceptance also
needed a plan strong enough to pass the conservative final gate without changing
the production policy.

**Decision:** Phase 5 uses two complementary checks.

- `tests/test_phase5_acceptance.py` starts the real FastAPI app against a migrated
  temporary database and a deterministic vendor. It drives authentication, batch
  analysis for `NSE:RELIANCE`, `NSE:TCS` and `NSE:HDFCBANK`, the Strategy route,
  execution preview/placement, fill and reconciliation through HTTP.
- The vendor returns monotonic bars for equities and the three index regime
  inputs. No network call is made and no model key is configured, so every LLM
  node is visibly degraded / `RULES_ONLY`.
- The monotonic fixture reaches final-gate conviction 3. The production default
  abstain band is `(2, 3)`, so the test owner explicitly saves
  `abstain_score_band=(2, 2)` before the app starts. This is a test-owner setting
  on a temporary database, not a production default change.
- A separate manual smoke run on 26 November 2026 used live cached market data:
  RELIANCE passed the gate, while TCS and HDFCBANK were correctly vetoed by the
  heuristic gate at conviction 2. That result is recorded rather than “fixed”.
- The acceptance asserts wiring and arithmetic, not strategy profitability or
  model quality. It does not assert that the default gate should pass every
  synthetic trend.

**Consequences:** CI can prove the chain without network or credentials. The
explicit policy override is visible in the test and must not be copied into
production defaults. Live acceptance remains a manual smoke step because vendor
and model availability are external.

## ADR-032: Cancellation and exits reuse the paper order and fill path

**Status:** Accepted; Phase 6 implements it.

**Context:** Phase 4B made paper orders fillable, but a pending order could not be
cancelled and a filled position could not be closed. Adding a second exit model
would duplicate the ledger, position and reconciliation logic that already exists.

**Decision:** Cancellation and exits are deterministic operations over the
existing order and fill tables.

- `POST /api/v1/paper/orders/{id}/cancel` calls the existing store cancel. It
  accepts only PENDING or PARTIALLY_FILLED orders; a filled or already-cancelled
  order is rejected.
- `POST /api/v1/paper/accounts/{aid}/positions/{instrument}/exit` creates a SELL
  LIMIT order for a quantity no greater than the open position. It uses the
  existing `place_order` transaction, so a short is impossible and an
  over-hold is rejected before any order exists.
- The exit order then uses the same fill API. The fill transaction credits the
  exact-paise ledger and reduces the position; no new ledger or matching model is
  introduced.
- Client order ids make the exit creation idempotent. The endpoint checks for an
  existing order before the position check, so retrying after a completed exit
  returns the original order rather than an over-hold error.
- Exits are owner-triggered and are not routed through the BUY pre-trade gate.
  Selling held paper shares reduces exposure; the gate remains the entry path.
- The Trade tab exposes per-position exit quantity and an explicit price, plus
  Cancel and Fill actions. There is no market order, short sale or broker call.

**Consequences:** A paper position can now be opened, filled, cancelled before
fill, partially exited, or fully exited through the UI. Realised P&L is reflected
in the account balance and ledger, not stored as a separate exit record. The
pre-trade gate and broker boundary remain unchanged.

## ADR-033: Risk-limit presets pre-fill, never silently decide

**Status:** Accepted; Phase 7 implements it.

**Context:** The Trade tab asked the owner for six gate limits on every run.
Repeating them is error-prone, but defaulting them would be worse: an invented
limit is an invisible policy change. The limits are gate inputs, not strategy
configuration.

**Decision:** Persist an optional preset and keep the request authoritative.

- `risk_limits` is an optional JSON value in `app_settings`. A fresh install has
  no preset; `AppConfig.risk_limits` is `None`.
- The value is validated by the same `RiskLimits` contract used by the gate:
  non-negative integer paise/counts, with at least one allowed order per minute.
- `GET/PUT/DELETE /api/v1/settings/risk-limits` are owner-only; writes require
  origin and CSRF. Reads are no-store. The response reports `configured: false`
  with null fields until the owner saves.
- The Settings card saves or resets the preset. The Trade tab loads it and
  pre-fills the six fields, but every preview and placement still carries the
  limits explicitly. The owner may override any field for one run.
- Nothing auto-places an order from a preset. The preset does not change the
  code-owned gate thresholds, sizing or risk policy.

**Consequences:** Repeating the limits is cheaper without hiding them. The preset
stores only the request inputs; a stale preset cannot silently alter policy. A
malformed stored value is ignored (`None`) rather than trusted.

## ADR-034: Semantic documents are owner-supplied and immutable

**Status:** Accepted; Phase 8 implements it.

**Context:** Semantic search existed over immutable documents, but only code could
add a document. The owner needs to store research text without turning ingestion
into an unbounded crawler or a model summarisation path.

**Decision:** Ingestion is an explicit owner action with a narrow contract.

- `POST /api/v1/memory/semantic` accepts symbol, HTTPS source URL, title, body,
  published time and optional known time. Writes require owner auth, origin and
  CSRF; reads remain no-store.
- The store validates every field and the URL (HTTPS, no embedded credentials).
  `known_at` cannot precede `published_at` or be in the future; oversized fields
  are rejected.
- No web fetch and no model call occur. The owner supplies the text; it is stored
  as an immutable document with a content hash and deterministic term index.
- Duplicate symbol/URL/published/content identity returns the existing document
  rather than creating a second row.
- The Agent Lab Semantic memory panel adds an ingest form beside search and
  refreshes the current search after a successful store. It shows the stored
  title and doc id.
- Semantic memory still excludes live quotes, OHLCV candles, positions and
  margins.

**Consequences:** The owner can make the memory layer useful without a crawler or
token spend. Ingestion quality depends on the supplied text; source metadata is an
owner assertion, not independent verification. Retrieval remains point-in-time.

## ADR-035: The worker executes registered jobs; the resolver is opt-in

**Status:** Accepted; Phase 9 implements it.

**Context:** The scheduler records wake decisions and enqueues `JobRun` rows, but
nothing executed them. The Brier loop still required the owner to press a button.
The job store already has lease tokens, fencing, retry and a bounded attempt
count; adding a second queue would duplicate that safety.

**Decision:** Add one bounded worker over the existing job store.

- `JobWorker` claims one available `JobRun` with `claim_next`, invokes exactly one
  registered handler by `job_id`, and calls `succeed` or `fail` with the held
  lease token. Missing handlers and handler exceptions become failed runs, not
  crashed loops.
- `JobWorkerLoop` runs the synchronous worker in a thread on a fixed interval and
  is started/stopped by the FastAPI lifespan beside the scheduler loop.
- `memory.resolve` is the only registered handler. It builds the existing
  analysis vendor router and calls `OutcomeResolver` with a 5-day horizon and a
  bounded episode count at the occurrence's scheduled time. It makes no model
  call.
- The daily schedule does not exist on a fresh install. `GET/PUT
  /api/v1/schedules/memory-resolve` reports and creates/toggles the
  `memory-resolve-daily` schedule; enabling it is an owner action. The default is
  disabled until then.
- `/api/v1/schedules/status` now includes recent job runs and attempt/failure
  state. The existing schedule panel shows them; there is no parallel scheduler
  surface.

**Consequences:** Due episodes can resolve without a manual button, and retries
follow the existing lease/attempt rules. A scheduled resolver still needs bar
data, so a vendor outage fails the job and leaves the episode unresolved for the
next occurrence. Working memory remains separate and deferred.

## ADR-036: Realised P&L is derived from average cost, not a second ledger

**Status:** Accepted; Phase 10 implements it.

**Context:** An exit fill updated cash and the position but did not show whether
the trade made or lost money. Adding a P&L ledger or a stored column would create
a second accounting source and a migration for a value that can be derived from
fills.

**Decision:** Compute realised P&L at fill time and return it.

- For a SELL fill, replay prior fills for the account/instrument with average-cost
  inventory. `realized_pnl_paise = proceeds - cost basis removed`. A partial exit
  keeps the remaining basis; a full exit clears it.
- BUY fills return `realized_pnl_paise = null`. A SELL with no prior cost basis
  fails closed before the fill is committed.
- The value is returned on `PaperFill` and the fill API response but is not stored
  in `paper_fills`. On an idempotent duplicate, the store replays fills up to the
  original fill id and recomputes the same result.
- The Trade notice shows a signed rupee value, including a loss. The ledger
  remains the cash authority; reconciliation is unchanged.
- Amounts are integer paise. No migration, no broker call, no model call.

**Consequences:** The owner can see the realised result of an exit without a
second accounting model. The P&L depends on average-cost replay; if a later
corporate action changes the basis, the historical fills remain the record and the
result is still deterministic. A dedicated realised-P&L report can query fills
later if needed.

## ADR-037: Cloudflare quick tunnel is runtime-only, owner-triggered egress

**Status:** Accepted.

**Context:** The owner wants to share a live session from another device without
deploying the app. The API is deliberately loopback-only: `TrustedHostMiddleware`
admits `127.0.0.1` only, `verify_origin` compares against a fixed
`http://127.0.0.1:8000`, and the session cookie is not `Secure`. A raw tunnel
would 400 on the Host header and 403 on every write, including login.

**Decision:** Add an owner-triggered Cloudflare **quick** tunnel that lives only as
long as its child process; nothing is persisted and there is no migration.

- `bq.infra.tunnel.TunnelManager` owns the state machine
  (`stopped → downloading → starting → live → stopping → stopped`, plus `error`).
  It spawns `cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate` with
  `asyncio.create_subprocess_exec` (no shell), parses the first
  `https://<id>.trycloudflare.com`, and marks `live` only after
  `GET /api/v1/system/health` through the tunnel returns `{"status":"ok"}`.
- The host and origin gates become **dynamic**: `TunnelHostMiddleware` admits
  loopback plus `manager.allowed_hosts()`, and `verify_origin` admits
  `ALLOWED_ORIGIN` plus `manager.allowed_origins()`. Both allowlists are empty
  unless the tunnel is `starting`/`live`, so the default posture is unchanged and
  every rejection fails closed.
- The session cookie is `Secure` only when the login's `Origin` is an active
  tunnel origin; loopback logins are unchanged.
- Access stays **owner password only**. No second auth path, no share token.
- The `cloudflared` binary resolves from `BQ_CLOUDFLARED`, then `PATH`, then
  `BQ_HOME/bin`. An explicit owner Start may download the official GitHub release
  into `BQ_HOME/bin`, size-bounded, atomically written and verified with
  `--version` before use. No owner-supplied URL is ever fetched.
- Shutdown (`lifespan` `finally`) stops the tunnel, so no child is orphaned.

**Consequences:** The link is public on the internet for as long as the tunnel
runs and is protected only by the owner password and the app's own auth, CSRF and
origin checks. Stopping the tunnel kills the link immediately. Auto-download is a
supply-chain surface accepted for convenience; the binary comes only from
Cloudflare's GitHub release and is version-checked. This does not weaken ADR-001
or ADR-013: it exposes the same paper-only app, it does not add a broker path.

**Amendment 2026-11-26 — liveness is local, not public-DNS dependent.**

*Problem observed in the field:* the tunnel reached "live" at Cloudflare within
~5 s and cloudflared logged `Registered tunnel connection`, yet the app always
ended in `error`. The health probe called
`https://<id>.trycloudflare.com/api/v1/system/health`, and a `*.trycloudflare.com`
name created seconds earlier is negatively cached by many ISP resolvers. Measured
on the affected network: the system resolver returned NXDOMAIN for ~80 s while
Cloudflare DoH resolved the same name in ~16 s. The probe therefore failed for the
whole `HEALTH_TIMEOUT_SECONDS` window and reported a healthy tunnel as broken.

Changes:

- `_read_ready` treats cloudflared's `Registered tunnel connection` line as the
  liveness signal (DNS-independent) instead of only the URL banner.
- The health probe targets the **loopback origin** (`local_origin`), proving the
  app behind the tunnel answers, rather than the public hostname.
- `START_TIMEOUT_SECONDS` 25 → 60 s; `HEALTH_TIMEOUT_SECONDS` 30 → 20 s;
  `HEALTH_POLL_SECONDS` 2 → 1 s.
- Failures append the last cloudflared `ERR`/`failed` line to `error`, so the cause
  is visible in the UI instead of a generic message.
- Log lines are bounded by `MAX_LOG_LINE_CHARS` (600) rather than reusing the
  500-char error bound, so cloudflared banners are no longer truncated mid-word.
- `request_start` is a no-op while `LIVE`, preventing a second child that would
  orphan the first.
- The panel polls until a terminal state with a bounded cap, and keeps the last
  known status through transient fetch errors; a live URL also shows a note that
  the owner's own resolver may need a minute to see the brand-new hostname.

Public reachability is unchanged in what it gates: the host/origin allowlists are
still populated only while the tunnel is `starting`/`live`. This only changes how
"live" is decided.

## ADR-038: Working memory is append-only decision state, not a second ledger

**Status:** Accepted; Phase 11 implements it.

**Context:** The run ledger stores a 64 KiB `detail_json`, but there was no
explicit, typed record of the state the coordinator actually had while deciding.
Confidence reviews and reports need a reproducible state that can be read without
re-running the pipeline.

**Decision:** Add one append-only working-memory row per `(run_id, symbol)`.

- Migration `031` stores run id, symbol, source, as_of, schema version, a bounded
  `state_json`, its canonical hash and the recorded time. Update and delete are
  blocked by triggers.
- `WorkingState` carries the data provenance, technical summary, model-routing
  snapshot, specialist briefs, evidence result, debate/verdict/risk, final-gate
  outcome, proposed plan, vetoes and bounded notes.
- The key includes the symbol because a Strategy run contains several symbols
  under one strategy run id. Single and batch runs normally have one symbol.
- Single, batch and Strategy paths persist after the run ledger. A memory failure
  is isolated and never breaks the analysis response.
- The state excludes raw quotes, OHLCV candles, open positions and margins.
- The owner-only, no-store read route is
  `GET /api/v1/analysis/runs/{id}/working-memory`; Technical and Strategy show it
  in an expandable panel. No model call is made to read it.

**Consequences:** The last original memory layer is present and reviewable. It is
an audit/read model, not an accounting or trading ledger; `analysis_runs` remains
the run summary and the paper ledger remains the cash authority. The 64 KiB cap
can drop an oversized optional state, but never corrupts a run.

## ADR-039: Confidence v1 is deterministic and deliberately narrow

**Status:** Accepted; Phase 12 implements it.

**Context:** The plan's confidence card promises calibrated probabilities and
seven evidence families. The repo already has technical and specialist facts but
no market-wide base-rate history, no relative-strength/peer series and no catalyst
model. Inventing those numbers would be worse than shipping a narrow, honest card.

**Decision:** Build a deterministic confidence v1 before any report or
opportunity surface.

- `bq/analysis/confidence_engine.py` produces evidence for four families:
  `STRUCTURE`, `VOLUME`, `MOMENTUM` and `MARKET_CONTEXT`, from the existing
  technical report and market-regime brief. Structure and volume remain mandatory;
  the policy still requires four confirming families.
- `bq/compute/setup_study.py` computes a symbol-scoped trend-continuation base
  rate from bars already available at decision time. Entry is the next bar's open;
  exit is a 1.5×ATR stop, 2.5×ATR target or the horizon close; a stop wins a
  same-bar tie. No look-ahead is used.
- Migration `032` stores append-only `setup_base_rates`. v1 labels the regime
  `ALL`; a market-wide, regime-conditioned study is deferred.
- `assess_confidence` applies the existing policy: minimum base-rate samples,
  mandatory families, 4-of-7 confluence, EV/R floor, provisional haircut and
  vetoes. `confidence` runs as a trace node after the final gate, never changes
  the plan or places an order.
- The run payload, working memory and Technical tab carry the assessment. No
  model call is involved; `confidence.review` remains deferred.

**Consequences:** The system can show a conservative confidence card without
faking calibration. Many symbols will be discarded for insufficient history or
missing volume/regime confirmation. The next phase can use these assessments for
ranking while a later market-wide study supplies true regime-conditioned rates.

## ADR-040: Opportunities are immutable; lifecycle and shadow outcomes are events

**Status:** Accepted; Phase 13 implements it.

**Context:** The existing opportunity row was a frozen confidence snapshot with
no lifecycle and no counterfactual learning. Updating it in place would destroy the
audit trail; adding a second candidate store would duplicate the confidence card.

**Decision:** Keep `opportunities` immutable and append the moving parts.

- Migration `033` adds `opportunity_events`, `shadow_trades` and
  `shadow_outcomes`, plus detail columns on the opportunity row. Update/delete
  triggers protect the event and outcome tables.
- Lifecycle is `NEW → WATCHING/TRIGGERED → TRADED → CLOSED`, with `DISMISSED` and
  `EXPIRED` terminal paths. The latest event is the current state.
- Every analysis/Strategy candidate with a confidence card is recorded, including
  non-suggested candidates. A shadow trade stores the plan geometry,
  `suggested` flag, confidence level and why it was not taken.
- The v1 score is a deterministic ranking formula over probability, EV, confirming
  families and vetoes. Confidence still decides suggestion; the score only orders
  the board.
- Shadow outcomes resolve at 1/3/5/10 days from later daily bars, using
  stop/target first-touch with the stop winning a tie. The Filter Verdict compares
  suggested vs other resolved shadows with Wilson intervals at horizon 5.
- The board only transitions lifecycle or stages a plan in Trade. It never places
  an order.

**Consequences:** The board is auditable and the counterfactual loop starts
filling. v1 records all confidence candidates rather than only HIGH/VERY HIGH, and
shadow resolution is a manual owner endpoint; both can be tightened/automated
without changing the immutable row contract.

## ADR-041: Scheduler jobs are catalogue entries; run-now only enqueues

**Status:** Accepted; Phase 14 implements it.

**Context:** The scheduler had one handler and one hard-coded memory toggle. The
plan requires every recurring job to be owner-configurable, runnable and auditable,
without turning an HTTP request into an inline job execution.

**Decision:** Add a code-owned catalogue and keep the durable job store as the
only queue.

- `bq/infra/job_catalogue.py` lists only jobs whose handlers are registered:
  `memory.resolve` and `news.ingest`. Each entry carries class, defaults, wake
  policy and retry bound. UI times are configuration, not baked into handler code.
- `GET /api/v1/schedules` returns editable schedule versions; `PUT` creates or
  updates one through the immutable `ScheduleStore`; enable/dry-run/run/history
  are separate owner-only endpoints. Writes require origin + CSRF.
- Run now enqueues one `JobRun` with an occurrence key `run-now:<schedule>:<key>`.
  A duplicate key returns the existing run; no handler executes in the request.
- Dry run validates the saved schedule and enqueues nothing. History reads the
  existing `job_runs` rows; there is no second run store.
- The Schedules page exposes the catalogue, next occurrences, edit/save,
  enable/disable, dry-run, run-now and history. The worker remains the only
  executor.

**Consequences:** Owners can configure and operate jobs without code edits, and
manual runs remain durable and idempotent. Dependencies/timeout/catch-up validation
is limited to what `ScheduleStore` already enforces; richer dependency execution is
still a later scheduler refinement.

## ADR-042: Reports are immutable; predictions freeze before the window

**Status:** Accepted; Phase 15 implements it.

**Context:** The daily report is the product's memory of what it expected and what
happened. Rewriting a report or prediction destroys the accuracy record; letting
the model choose levels or probabilities makes scoring meaningless.

**Decision:** Keep reports and prediction metadata append-only, and compute the
v1 reports deterministically.

- Migration `034` adds immutable `reports` and `prediction_details`. The existing
  `predictions`/`prediction_outcomes` tables remain append-only. Corrections are
  new report rows, never edits.
- The morning engine reads daily NIFTY/VIX bars at `as_of`, computes regime,
  ATR-based range and probabilities in code, and freezes DIRECTION, RANGE, GAP,
  VOLATILITY and a 3-session Market Stance before the next session opens. No model call is on this path.
- The evening engine resolves every due prediction using code-owned rules
  (dead-band for direction/gap, containment for range, close comparison for VIX),
  writes the immutable outcome, and scores the run with the existing
  Brier/log-loss helpers. Missing bars are recorded as notes rather than silently
  dropped.
- Reports are stored as Markdown and JSON in SQLite and mirrored under
  `BQ_HOME/reports/YYYY/MM/DD/`; the content hash covers both. The API is
  owner-only, writes require origin + CSRF, and report reads are `no-store`.
- `report.morning` and `report.evening_accuracy` are catalogue jobs. Enabling a
  schedule remains an explicit owner action; run-now still only enqueues.

**Consequences:** Accuracy is auditable and predictions cannot be rewritten with
hindsight. v1 has no LLM synthesis, provisional/confirmed distinction,
LEVEL_TOUCH/RELATIVE_PERF/TRADE resolvers, or model attribution; these can be added
as new report versions without changing the immutable contract.

## ADR-043: Paper realism keeps cash, lots and approvals deterministic

**Status:** Accepted; Phase 16 implements it.

**Context:** The paper OMS had one product, no charges, no settlement and no
mandatory decision point for large orders. Adding realism inside the fill could
silently change historical accounting; letting the model decide costs or approval
would make the audit trail unfalsifiable.

**Decision:** Keep accounting deterministic and make every realism dimension
explicit.

- Migration `035` adds itemized `paper_charges`, `paper_tax_lots`,
  `paper_product_positions`, `paper_approvals` and `paper_stops`, plus product,
  approval and gross/charges/net columns on orders/fills.
- A code-owned charge schedule computes STT, exchange, SEBI, stamp and GST. Each
  component is stored. Ledger cash is gross +/- charges; realised P&L remains the
  average-cost gross figure, while net P&L subtracts sell charges plus allocated
  buy charges. The two are never conflated.
- Orders carry `MIS` (default, same-day) or `CAS`. CAS buys create tax lots with a
  T+1 settlement date; a CAS sell cannot fill until enough settled lots exist.
  Lots are a FIFO view only; the average-cost inventory remains the accounting
  basis.
- `paper.square_off` closes open MIS positions at the daily close.
- `paper.stops` evaluates active stop/target exits from daily bars. A stop is
  one-shot: it creates a durable SELL order and marks itself `TRIGGERED`; the
  trigger is idempotent. A failed fill leaves the order visible rather than
  retrying invisibly.
- Approval is explicit: the request may require it, and code requires it
  automatically when notional exceeds 50% of available cash. Pending orders
  cannot fill. Approval/rejection is owner-only, CSRF-protected and audited.

**Consequences:** Gross and net accounting are auditable, CAS/ MIS behaviour is
explicit, and large orders cannot bypass a human decision. v1 uses weekday T+1
(not the exchange holiday calendar), daily bars for stops (not intraday triggers),
and an estimated charge schedule; each can be refined without changing the
immutable fill and approval contracts.

## ADR-044: Strategy versions are immutable; fast/event parity is a gate

**Status:** Accepted; Phase 17 implements it.

**Context:** A strategy result has no meaning if the definition can be edited
afterwards, or if the fast research engine and the event engine disagree. Both
would make a review decision impossible to audit.

**Decision:** Separate definition, execution and review into immutable records.

- `strategy_versions` stores a strict DSL document and its canonical hash. The
  same `(strategy_id, version)` cannot be rewritten with different content or
  lifecycle. Seed strategies are code-owned and inserted idempotently.
- The DSL fixes the vocabulary for universe, entry/exit rules, ATR multiples,
  risk, slippage and knowledge cutoff. Unknown fields and out-of-bound values are
  rejected rather than defaulted.
- `fast` and `event` engines share the same rules and accounting but compute
  indicators differently (precomputed vs bar-by-bar). Tests compare their entire
  serialized output, excluding the engine label.
- Backtests use only bars at or before `as_of`, enter on the next bar open, use
  stop/target first-touch with the stop winning ties, and apply paper charges and
  slippage. Metrics keep gross, charges and net separate.
- `backtest_runs` are immutable and `strategy_reviews` append-only. Review is an
  owner action; the model never accepts its own run.

**Consequences:** A reviewed run is reproducible from its strategy hash and
archived trades. v1 is daily-bar, one position per symbol, without portfolio
margin, walk-forward optimization or an AI review layer; those can be added as
new engines without changing the immutable version/run contracts.

## ADR-045: Research notes are point-in-time; valuation arithmetic is code-owned

**Status:** Accepted; Phase 18 implements it.

**Context:** Research text and financial facts arrive at different times. A note
ingested today may describe an event that was not public yesterday; a valuation
that uses future-known facts contaminates the historical record. Letting the
model compute ratios or DCF would make the result unreproducible.

**Decision:** Separate evidence storage from deterministic arithmetic and human
judgement.

- Migration `037` adds append-only `research_notes`, `fundamental_snapshots`,
  `thesis_events`, `research_claims` and `research_claim_reviews`.
- Every note carries `published_at` and `known_at`. Queries take one `as_of`
  pivot and return only notes with both timestamps at or before it. Future
  `known_at` values are rejected. News items can be imported without refetching
  the article.
- A fundamental snapshot stores absolute paise values, period end and known-at.
  Ratios and DCF are computed in `bq/fundamentals.py` with Decimal arithmetic;
  the model never supplies or edits them. DCF assumptions are explicit and
  bounded.
- Theses stay immutable. Evidence, invalidation and reactivation are appended as
  `thesis_events`; a dossier shows the event history rather than mutating a
  thesis state.
- Research claims are text-only records. Acceptance/rejection is an append-only
  owner decision; a claim cannot be auto-accepted.

**Consequences:** A dossier is reproducible at any past `as_of` and fundamental
calculations are auditable. v1 accepts owner-supplied XBRL-shaped facts rather
than parsing XBRL documents, supports YouTube metadata/notes rather than video
transcription, and has no automatic thesis invalidation rule; each can be added
without changing the immutable note/snapshot contracts.

## ADR-046: Runtime status is read-only; restore is explicit

**Status:** Accepted; Phase 19 implements it.

**Context:** Operational checks must not become a second execution path. A backup
or event feed that mutates accounting, or a restore that silently overwrites the
live database, would invalidate the paper audit trail.

**Decision:** Keep runtime tooling read-only except for the explicitly confirmed
restore.

- `bq/runtime.py` reads DB/WAL sizes, migration count, job counts, stale leases,
  table counts and disk headroom. It never changes job state; lease recovery stays
  in `JobRunStore`.
- `/api/v1/system/status` and `/api/v1/system/metrics` are owner-only and
  `no-store`. `/api/v1/events` is an owner-authenticated WebSocket carrying
  bounded runtime snapshots, not a job queue or trading feed.
- Backups use SQLite's online backup API into a new file, verify integrity and
  write a SHA-256 manifest. Restore requires `--confirm`, validates the backup,
  copies the current DB to `bq.db.pre-restore`, and re-runs integrity/migration
  checks.
- `bq bench` measures deterministic hashing/timestamp parsing only; it is an
  operational smoke check, not a performance claim.
- Operational guidance lives in `docs/runtime-operations.md`; launchd starts one
  server/worker, and caffeinate does not create a second runtime.

**Consequences:** Operators can inspect and recover the runtime without bypassing
the lease or accounting contracts. v1 has no persisted WebSocket event log,
automated backup retention, Playwright E2E or external alerting; those are
maintenance extensions, not changes to the immutable data boundaries.

## ADR-047: Operational logs are file-backed, bounded and separate from `audit_events`

**Status:** Accepted; implemented by `bq/logbook.py`.

**Context:** Debugging model behaviour needs the full prompt, response and the
decision around it. The hash-chained `audit_events` table records state-changing
owner actions and must stay small; storing prompts there would bloat it and mix
telemetry with the audit contract.

**Decision:** Add a file-backed operational logbook separate from `audit_events`.

- Every LLM call is recorded at the transport choke points
  (`LLMService.complete`, `ModelRouter._complete_route`, and native Jev) with
  provider, model, task, agent, latency, tokens, outcome and the full
  system/prompt/response text.
- Every decision is recorded at the deterministic choke points: the model decision
  router, the final gate, campaign research, and the intraday trigger scan.
- Entries append to `.pending.jsonl` and flush to dated JSONL files every 15
  minutes by default. Three drivers exist: a record-time fallback, an asyncio
  loop in `bq serve`, and a `logs.flush` job.
- Text fields, entries and files are bounded. Retention is owner-configurable
  (1–3650 days, default 14). Bearer/API-key-shaped substrings are redacted.
- `/api/v1/logs` is owner-authenticated and `no-store`; log files live outside
  the static web root.

**Consequences:** The owner can reconstruct exactly what a model saw and answered
without touching immutable accounting or audit tables. Full prompts are stored in
plain text under the local app-data directory, so the UI says so plainly. Losing
logs cannot affect trading state or the audit chain.

## ADR-048: The LLM gateway governs task-bearing calls; generic text bypasses it explicitly

**Status:** Accepted; implemented by `bq/llm_gateway.py` and
`ModelRouter.complete` (registered tasks delegate to the gateway; generic text
keeps the synthetic route path). No task definitions are seeded yet, so the
governed path activates only once one is registered.

**Context:** `bq/llm_gateway.py` defines the intended authoritative offline
boundary: it loads a registered `TaskDefinition`, applies `select_model`
(ADR-005), persists the `BuiltContext` manifest, validates the output against the
task schema, verifies citation versions against `DataRecordStore` (ADR-004), and
records a `ModelAttempt`. Nothing in production currently calls it. Live calls go
through two independent paths — `ModelRouter._complete_route` and
`LLMService.complete` — each building a synthetic `ModelCandidate` and
`TaskPolicy` inline and then calling `client.complete`. The schema, citation and
manifest guarantees the gateway exists to enforce are therefore absent from every
real call, and two nearly identical routing blocks can drift.

**Decision:**

- `LLMGateway` is the only path for *task-bearing* calls: any call that names a
  registered `TaskDefinition` — the research dossier, evidence verification, the
  final decision gate, confidence review, and any future task with a
  `citation_path` or an output a deterministic gate consumes.
- Those paths call `gateway.prepare(...)` → provider call →
  `gateway.validate_output(...)` → the attempt recorded via
  `gateway.complete(...)`. `ModelRouter` and `LLMService` delegate to the gateway
  for them instead of constructing a `ModelCandidate`/`TaskPolicy` inline.
- Generic text completion with no registered task keeps the current synthetic
  route path (`select_model` + `client.complete`) and is documented as explicitly
  un-manifested: the interactive research loop's tool turns, `POST /api/v1/llm/complete`,
  and the debate/risk commentary that carries only bounded facts. `LLMService`
  remains a thin helper for that path.
- A task is registered only when its output is consumed by a deterministic gate
  or produces citations. Commentary that cannot change a trade does not get a
  manifest; output that can change a decision does.
- The decision ladder (native Jev → text tiers → heuristic, ADR-017/018) is
  unchanged. The gateway wraps the text-tier call, not the ladder.
- `bq/services.py` builds the gateway alongside `ModelRouter` so the API, CLI and
  scheduler share one instance.

**Consequences:**

- Decision-changing model output is schema-validated and citation-checked at one
  choke point, with the manifest stored before the call, so "what did the model
  see" stays answerable after the fact.
- The task-definition registry grows deliberately: a new task-bearing agent must
  register a `TaskDefinition` before it can reach a provider through the governed
  path. That is the point.
- Generic commentary stays cheap: no manifest, no citation lookup, no extra model
  call. Deterministic arithmetic, sizing, gates and risk remain in code.
- `tests/test_llm_gateway.py` extends to cover the delegated
  `ModelRouter`/`LLMService` path; `tests/test_model_router.py` confirms generic
  completion is unchanged.
- Risk to manage: the gateway and the router must resolve eligibility from the
  same configured candidates so a model allowed in one path is not silently
  rejected in the other.

**Alternatives rejected:**

- *Retire the gateway and keep only the synthetic path* — loses the schema,
  citation and manifest guarantees that ADR-004/005 promise for decision-bearing
  calls.
- *Force every call through a manifest* — adds cost and complexity to commentary
  that cannot change a trade, contradicting the cost-first posture and the rule
  that deterministic code owns the numbers.

## ADR-049: Local models are a configuration registry; eval configs are generated per run

**Status:** Accepted; implemented by `bq/local_models.py`, `SettingsStore`
(`app_settings.local_models`), `bq/evals/config.render_eval_config`, the
`/api/v1/models` routes, and the Model Evals / Settings panels.

**Context:** The three benchmarked models were hardcoded in
`evals/promptfooconfig.yaml`, so adding an open-source model meant editing a
tracked file and rebuilding nothing else. Promptfoo's `--filter-providers` can
only select providers already declared in a config; it cannot add one. Routing
also needed the same model to be selectable in Settings without retyping a
string. The registry must stay deterministic configuration, and the shipped YAML
must remain the golden-set template (prompts, tools, assertions, tests).

**Decision:**

- A model is a `LocalModel` id `ollama:chat:<model>` or
  `openai:chat:<model>`, stored as JSON in `app_settings.local_models` with a
  bounded shape (label, kind, model, endpoint, context/predict tokens,
  temperature, think). No migration. An explicitly emptied registry stays empty;
  only an absent key seeds the shipped YAML providers, so the original three are
  never silently lost or resurrected.
- Eval runs render a **per-run** `promptfooconfig.yaml` into the run artifact
  directory: only the top-level `providers:` block is replaced, the
  `x-trading-tools` anchor and assertions are preserved, and the dataset path is
  rewritten absolute because the generated file lives outside `evals/`. The
  shipped YAML is never mutated.
- Presence uses `ollama list`; `installed: null` means "unknown" (not Ollama, or
  the daemon is unreachable). Pulling is a single background `ollama pull` with
  bounded output, mirroring the eval runner's lifecycle.
- Local models route through `Provider.CUSTOM` (keyless when a loopback
  `custom_endpoint` is set) — that keyless endpoint now counts as configured.
  Selecting a registry model in a tier derives its endpoint. Only one local
  endpoint can be routed at a time; conflicting selections return `422`.
- Promotion validates against the registry, not the YAML, and writes the
  endpoint plus the agent override.

**Consequences:**

- A new model is added, pulled, evaluated and routed from the UI with no code
  edit and no hardcoded list.
- Each run is reproducible from its artifact config; historical runs keep the
  provider set they actually used.
- The registry is configuration, so it is bounded (32 models) and validated at
  the API boundary; pulling is the only heavy operation and reports progress.
- Risk: the CUSTOM client still resolves one global endpoint, so an Ollama model
  and a different OpenAI-compatible endpoint cannot be routed in different tiers
  simultaneously. Per-model endpoints would need a routing change (follow-up).

**Alternatives rejected:**

- *Append providers to the tracked YAML* — mutates a versioned golden-set file
  and makes runs non-reproducible.
- *Keep filtering the static YAML only* — cannot evaluate a model that is not
  already declared, which is the feature.
- *A new SQL table + migration* — the data is configuration edited by one owner;
  `app_settings` already stores tiers and routing in the same form.

## ADR-050: `aihf` runs from an isolated sidecar venv, driven as a subprocess

**Status:** accepted (28 Nov 2026). Adds the **AI Hedge Fund** tab.

**Context.** The request was to install `aihf` (PyPI, `virattt/ai-hedge-fund`
v2) and expose its mandates, backtests and paper desk in this app. Measuring
the distribution before writing any code changed the design:

- **The dependency conflict is hard.** `aihf==2.7.2` requires
  `pandas>=2.1,<3` and `numpy<2`. This project is locked to
  `pandas>=3.1,<4` (3.1.1) and `numpy>=2.5` (2.5.5). `uv` reports *"your
  requirements are unsatisfiable"*; there is no resolution that keeps both.
- **The import name is `hedge_fund`.** `aihf` is only the console script, so
  `import aihf` can never succeed regardless of installation.
- **It has no HTTP API.** It is a CLI with a Textual TUI. The machine surface
  is JSON on stdout from `aihf backtest` / `paper tick` / `paper scorecard`.
- **`aihf paper tick` has no dry run.** One call decides, executes and appends
  a hash-chained record.

**Decision.**

1. **A sidecar virtual environment** (`.aihf-venv`, provisioned by
   `just aihf-install`), invoked through `asyncio.create_subprocess_exec` with
   `start_new_session=True` and `killpg` escalation. `bq` never imports
   `hedge_fund`, so the core lock is untouched and the two pandas versions
   cannot collide.
2. **Settings remain the single source of truth.** Each run gets a scoped
   `env` built by `get_aihf_runtime_env`, and every variable it owns is
   removed from the inherited environment first. `aihf` loads its own `.env`
   with `override=False`, so an injected variable always wins — verified
   against `hedge_fund/tui/keys.py`, not assumed.
3. **A private `HOME` per run** (`<BQ_HOME>/aihf/home`). aihf resolves
   mandates, paper, research, cache *and* `.env` through `Path.home()`, so this
   confines it to state this app owns and makes a stray `~/.hedge-fund/.env`
   unreadable.
4. **Approval by adoption.** Because a tick cannot be previewed, a preview runs
   that same tick against a *copy* of the fund, and approval copies aihf's own
   ledger/broker/events files back over the live fund. Nothing is
   re-serialised: aihf's `Ledger.replay` re-verifies every hash on the next
   read, so an adopted record cannot be forged. The kill switch is checked
   *before* preview state, and the ledger head is re-checked before adoption.
5. **The Financial Datasets key is not a provider key.** `provider_api_keys`
   has a `CHECK` over provider names, so adding one would force a migration.
   It lives in its own `app_settings` row (`aihf_secrets`), is masked on read,
   and is deliberately **not** on `AppConfig`, so it cannot ride along in any
   existing settings projection.
6. **Honest progress.** `aihf backtest` prints nothing until it finishes, so
   the SSE journal streams real phases and an elapsed clock rather than an
   invented per-session percentage.

**Consequences.**

- The sidecar is not in `uv.lock`, so `test_dependency_notices.py` is
  unaffected; `README` documents the provisioning step and `.gitignore` covers
  the venv.
- The LLM model list comes from aihf's registry and, because `make_llm`
  derives the provider *from the model id*, an unlisted id would silently go to
  Anthropic. Overrides outside the registry are therefore refused (422), and a
  model is only selectable when its provider's key is bridged.
- Bridging is limited to what both sides share: `GEMINI→Google`,
  `OPENAI→OpenAI`, `DEEPSEEK→DeepSeek`, `TYPESAFE_JEV→TypeSafe`. BharatQuant
  stores no Anthropic/xAI/Kimi key, so those aihf providers are listed but not
  runnable.
- aihf's registry ids are placeholders (`claude-opus-5-5`, `gpt-6-sol`, …).
  A first real run may need an id the vendor actually serves.

**Alternatives rejected.**

- *Install into the app venv and downgrade pandas* — rewrites a locked core
  dependency that backtests, features, campaign, technical analysis and
  reports all build on.
- *Reimplement aihf's engine in-tree* — large, and drifts from upstream;
  the value is its real pipeline.
- *Preview by replaying the ledger narrative only* — cheap, but the approval
  modal could not show the orders it is asking you to approve.

## ADR-051: AWS Bedrock is an additive provider using a bearer API key

**Status:** accepted (28 Nov 2026).

**Context.** The owner wanted Bedrock available as a model provider. Three
facts decided the design, all verified against AWS's documentation:

- Bedrock issues **API keys that are bearer tokens** (`Authorization: Bearer`,
  or `AWS_BEARER_TOKEN_BEDROCK`). They are not AWS secret access keys, and no
  SigV4 signing is involved.
- There are two kinds: **short-term** (≤12 hours, locked to the region it was
  minted in, inherits the generating principal's permissions) and
  **long-term** (until an expiry date, backed by a purpose-made IAM user with
  `AmazonBedrockLimitedAccess`). AWS recommends short-term for production and
  describes long-term as exploration-grade.
- Bedrock's model-neutral request format is **`Converse`**, which is a third
  wire shape here (neither Gemini's `generateContent` nor OpenAI chat).

**Decision.** Bedrock is a **new provider member alongside the existing five**,
not a replacement: every current provider, its stored key and the active
selection are untouched (asserted by test).

1. **Bearer token, stored like any other key.** The owner chose a long-term API
   key held in Settings, consistent with every other provider. Accepted cost,
   recorded here rather than glossed: a long-term key creates an IAM user in
   the AWS account, and short-term/SigV4 remain the better production answer.
2. **A `BedrockClient` speaking `Converse`.** One client, because `Converse` is
   model-neutral. The model id is validated against `[A-Za-z0-9._:-]` rather
   than path-escaped: it lands in the URL path, so a slash would silently
   address a different resource.
3. **Region is configuration.** It is interpolated into a hostname, so
   `validate_bedrock_region` accepts only a bare AWS region name — arbitrary
   text would let stored configuration choose the host. Default is `us-east-1`.
   It is stored separately from the key so it can be corrected without
   re-entering the key.
4. **Migration 041** rebuilds `provider_api_keys` to widen the CHECK, the same
   copy/drop/rename as 026. Verified as an **incremental** upgrade: an existing
   database went 40 → 41 with a pre-existing GEMINI key intact.
5. **`parse_usage` learns Bedrock's shape.** `Converse` reports camelCase
   `inputTokens`/`outputTokens`. Without this, every Bedrock call would have
   been recorded as *unreported* usage — a silent hole in cost and
   confidence accounting that the tests caught.

**Consequences.**

- Bedrock is a legitimate reasoning/flash tier provider; it is **rejected on the
  decision tier**, which accepts only TypeSafe Jev. The tier validator was
  already exclusion-based, so no change was needed there.
- Model ids are region-namespaced (`us.anthropic.claude-sonnet-4-6`). The
  Settings placeholder shows that form.
- A 403 from Bedrock (bad or expired key) surfaces as HTTP 502 with the status
  only; the response body is never echoed, since it can contain key fragments.
- Tool/function calling is **not** implemented for Bedrock: `Converse` exposes
  `toolUse`, which the pipeline does not send. Text completion is the supported
  surface.

**Alternatives rejected.**

- *Short-term keys auto-refreshed via `aws-bedrock-token-generator`* — no
  phantom IAM user, but adds a dependency and requires working AWS credentials
  on the host; the owner preferred the settings-only model.
- *SigV4 with a profile/role or boto3* — the conventional AWS answer, but it
  moves credentials outside Settings and adds a heavy dependency.
- *Reusing `OpenAICompatClient`* — `Converse` is not OpenAI-shaped; the request
  and response differ structurally.
