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
