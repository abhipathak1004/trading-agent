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
