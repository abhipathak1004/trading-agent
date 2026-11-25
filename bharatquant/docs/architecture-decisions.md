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
