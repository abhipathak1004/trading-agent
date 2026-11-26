# BharatQuant

BharatQuant is a local-first Indian-markets research and paper-trading project.
It currently provides an authenticated foundation workspace, an offline EOD
CSV importer, and import-status, snapshot-freshness, calendar-coverage,
schedule-status, paper-account and model-usage views. Live market providers,
trade pages, accounts management, orders and trading features are not
implemented.

The frontend follows the companion
[UI/UX Development Guide](../BharatQuant%20UI-UX%20Development%20Guide%20v1.0.md)
for the app shell and non-negotiable UX rules. Settings, Agent Lab and the
command palette remain planned; navigation is limited to implemented
foundation capabilities and does not imply that unavailable features exist.

## Requirements

- Python 3.13
- Node.js 22.12 or newer supported by Vite, plus npm
- `uv`
- Optional: [`just`](https://just.systems/) for the repository shortcuts

## Quick shortcuts

From the repository root, run `just` to list recipes:

```sh
just install
just run
```

`just run` builds the web app, initializes local storage without overwriting
existing data, and starts the loopback server. On first use, complete owner
setup in the browser or run `just auth-setup` in a terminal.

Other useful recipes include `just build`, `just doctor`, `just test`,
`just check`, `just news` and `just web-dev`. `just web-dev` starts only the
Vite frontend; owner sign-in is not supported on its dev origin.

Recipes export `PYTHONPATH` to the source tree. On macOS, Python skips any
`.pth` file carrying the hidden file flag, and uv's editable install can pick
that flag up inside `.venv`; without the export, commands fail with
`ModuleNotFoundError: No module named 'bq'` even though the package is
installed. `pytest` is configured the same way.

## Manual build and run

From the project directory:

```sh
uv sync --locked --python 3.13
cd web
npm ci
npm run build
cd ..
uv run --locked bq init
uv run --locked bq auth setup
uv run --locked bq serve
```

Open `http://127.0.0.1:8000`. The server binds to loopback only. On non-macOS
development hosts, set `BQ_HOME` to a dedicated local data directory before
running `bq init`; macOS uses the BharatQuant Application Support directory by
default.

While the server is running, its 30-second scheduler discovery loop records due
wake decisions and durably enqueues `RUN` decisions as job occurrences. It does
not execute job handlers or create an operational trading workflow.

On a new database, open the local app and use the first-run owner setup wizard.
The wizard requires password confirmation and rejects common, repetitive or
short passwords; setup is allowed only from the loopback origin and is
recorded in the append-only audit chain. The CLI fallback is
`bq auth setup`; it uses the same server-side password rules and requires a
terminal that can disable echo. Password reset and recovery are not implemented.

Authentication setup, successful sign-ins, lockouts and sign-outs are written
to an append-only, hash-chained local audit log. `bq doctor` verifies the
chain. This detects accidental or partial modifications; it is not tamper-proof
if an attacker can rewrite the database and recompute the entire chain.

The local server stores an Argon2id password hash and uses a host-only
HttpOnly, SameSite=Strict session cookie. Sessions expire after 12 hours;
five incorrect sign-in attempts lock sign-in for 15 minutes. The loopback
server uses HTTP, not TLS; browser cookies are not isolated by port, so
do not treat unrelated services on the same host as a security boundary.
The SPA uses a per-response nonce-based Content Security Policy. Authentication
protects the import-status, snapshot-freshness, calendar-coverage,
schedule-status and model-usage endpoints, which are not cached.
Protect every new private API and state-changing request before adding it.

`npm run dev` starts Vite for frontend work and proxies `/api` requests to the
local backend on port 8000. Build the web app before running `bq serve`; the
build is packaged with the Python application. The server refuses to start
if the build's `index.html` is missing. Owner sign-in requires the served
`http://127.0.0.1:8000` origin and is not supported through Vite's dev origin.

### Share over a Cloudflare tunnel

Settings → **Cloudflare tunnel** can start an ephemeral
`https://<id>.trycloudflare.com` quick tunnel to the running server, so a second
device can reach it while `bq serve` is running. Nothing is persisted; the link
dies when you press **Stop tunnel** or quit the server.

- Access is still **owner password only**. The link is public on the internet, so
  treat it like any shared credential and stop the tunnel when finished.
- The default posture is unchanged: with no tunnel active, only `127.0.0.1` is
  accepted and non-loopback Host/Origin requests are rejected. While a tunnel is
  live its host and origin are allow-listed, and the session cookie is `Secure`.
- `cloudflared` resolves from `BQ_CLOUDFLARED`, then `PATH`, then `BQ_HOME/bin`.
  On the first Start the app may download the official release into `BQ_HOME/bin`
  (verified with `cloudflared --version`) instead of asking you to install it.
- The app marks the URL live once cloudflared has registered its edge connection
  and the local origin answers its health check. It deliberately does not wait on
  the public hostname: a brand-new `*.trycloudflare.com` name is negatively cached
  by many ISP resolvers for a minute or more, so your own browser may need a short
  wait before the copied link opens even though the tunnel is already up.

This is remote access to the same paper-only app. It adds no broker path and does
not change ADR-001 or ADR-013.

## Checks

```sh
cd web
npm test
npm run typecheck
npm run build
npm audit
cd ..
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q
```

The frontend contract tests use Node's experimental TypeScript stripping.

## Development skills

Copilot's repository instructions are in
[`.github/copilot-instructions.md`](../.github/copilot-instructions.md), with
project skills in [`.github/skills/`](../.github/skills/). Use Clean Software
Craftsmanship for behavior, boundaries, maintainability and verification;
Ponytail guidance to keep changes minimal but complete; and Graphify when
cross-file relationships or change impact are useful. The full Clean Software
Craftsmanship source remains in
[`clean-software-craftsmanship/`](../clean-software-craftsmanship/).

Graphify is an optional local developer tool, not an application dependency.
For broad code-relationship questions, query an existing `graphify-out/`
graph first. If the local CLI is installed and the graph is missing or stale,
`graphify extract . --code-only` builds a local AST graph without LLM
extraction; `graphify-out/` is git-ignored. Verify graph findings in source and
tests. Do not upload this codebase to a hosted graph service. The repository
instructions describe Copilot's Graphify workflow; they do not install
Graphify or Ponytail plugins or enable Ponytail slash commands. Install the
optional Graphify CLI with `uv tool install --python 3.12 graphifyy`; its VS
Code registration command is `graphify vscode install`.

The health endpoint is a liveness check only. No provider, exchange-rule,
investment-performance or macOS hardware claims have been verified. See
[external-source gates](./docs/external-source-gates.md) for the current
verification blockers and [architecture decisions](./docs/architecture-decisions.md)
for the approved design constraints.

An isolated fixed-fractional long-sizing calculation accepts caller-supplied
equity, cash, entry and stop prices in paise and a risk fraction in basis
points. It floors the budget and limits whole shares by nominal stop distance
and gross cash. It does not account for fees, slippage, gaps or short margin;
realized loss can exceed the nominal stop risk. It sets no risk defaults and
is not an order authorization or an operational risk gate.

An offline binary-scoring helper calculates Brier score, log loss and a 95%
Wilson interval for the observed positive-outcome rate from caller-supplied
resolved outcomes. An impossible outcome at probability zero or one produces
an explicitly unbounded log loss, not an arbitrary clipped value. The Wilson
interval describes the sampled rate, not an individual forecast. A separate
calibration summary uses caller-specified bin edges from zero to one; each bin
includes its lower edge and excludes its upper edge except that the last
includes one. Empty bins have no averages. A separate accuracy calculation
requires an explicit positive-class threshold and includes its own Wilson
sample interval. The helper
can also compare paired forecasts against a caller-supplied baseline,
rejecting mismatched outcomes; it cannot verify that pairs identify the same
prediction. A dated scoring helper excludes predictions created at or after
their window starts and waits until the supplied resolution time is known by
`as_of`; it rejects duplicate IDs and future-created predictions. These dates
are caller assertions, not a persisted freeze or proof of source availability.
The helpers do not establish provenance, planned-prediction coverage,
small-sample labels or correctness and are not connected to reports.

An offline snapshot-freshness diagnostic compares event time with `as_of`
using caller-supplied delayed and critical thresholds. Delayed data can be
reported as `WARN`, `BLOCK` or `ALLOW`; `ALLOW` still labels the data delayed.
Critical staleness, non-acceptable quality and missing event times block even
when `ALLOW` is selected. Thresholds are exceeded strictly, not at equality.
The diagnostic rejects records unavailable by `as_of` and future event times.
It also rejects event times after publication or receipt and record times
before knowledge time.
Quality, timestamps and source identity remain assertions; the diagnostic is
not connected to a provider, readiness check or order authorization.
To inspect an existing record, run `bq data inspect-snapshot RECORD_ID --as-of
TIMESTAMP --delayed-after-seconds 60 --critical-after-seconds 180
--delayed-mode WARN` with thresholds chosen for that use. The CLI accepts
thresholds up to one year, uses local OS permissions rather than the web
password, and returns metadata and age rather than snapshot payloads.
The signed-in snapshot panel and `/api/v1/data/snapshot-freshness` offer the
same diagnostic for an explicitly selected retained record and thresholds.
The panel uses the device clock as `as_of`; it shows source, quality,
timestamps, age and the selected policy. The endpoint is read-only and
non-cacheable and does not return the snapshot payload. No snapshot provider
or record-discovery UI is implemented.

The calendar foundation stores date-specific, owner-reviewed session records
with source and knowledge-time provenance, and resolves only entries known,
reviewed and recorded by the requested `as_of` time. It contains no seeded
exchange data and raises an explicit uncovered-date error rather than assuming
a date is open. The signed-in calendar panel checks up to 31 inclusive dates
for an explicitly entered exchange code. The authenticated, non-cacheable
`/api/v1/calendar/coverage` endpoint returns the version known, reviewed and
recorded by the supplied `as_of` time, or `UNCOVERED`; all dates are read from
one database snapshot. The panel uses the device clock for `as_of` and displays
the time. Owner review and timestamps are assertions, not independent
verification. This view cannot edit calendar records or establish readiness.

After independently reviewing the authoritative source and its applicability,
the owner can record one session from a local terminal:

```sh
uv run --locked bq calendar record-session --exchange-code YOUR_EXCHANGE \
  --date 2026-11-23 --class REGULAR --state OPEN \
  --opens-at 2026-11-23T03:00:00Z --closes-at 2026-11-23T10:00:00Z \
  --source-url https://example.test/calendar --confirm-review
```

Replace every example with the reviewed source and actual session values;
the example is not an exchange rule. For a closed session omit the opening
and closing timestamps; add `--source-published-at` when known. Timestamps
must include a timezone. The command records knowledge and review at the
local current time rather than backdating, and appends an audit event. It uses
local OS permissions, not the web password. Confirmation is an owner
attestation, not independent verification; no exchange data is seeded.

The data foundation also stores append-only, provider-neutral records with
source, unit, event/publication/receipt/knowledge timestamps, quality status,
canonical payloads and an explicit `as_of` lookup that excludes records saved
after that time. The migration seeds no facts; tests use synthetic fixtures.
There is no provider adapter or public API, and the contract does not establish
that any source, market fact or quality assessment is verified. Record times
depend on the local system clock. They are captured during the write
transaction, not a separately trusted commit-time checkpoint; consumers needing
exact replay must retain immutable input references.

An internal provider contract accepts 1–256 distinct exact record keys, one
record kind and an aware `as_of` time. It checks declared capabilities before
fetching, requires one matching record from the declared source per key, and
rejects duplicates, missing records and records known or received after
`as_of`. Results follow request order. The contract does not implement a live
provider, calendar operation, pagination, range queries, retries, rate limits
or fallback. Source identity, capabilities and historical timestamps are provider
assertions, not verified evidence: a returned record is not proof that this
application possessed it at `as_of`. Future ingestion must retain record
versions and their local record times, and enforce source and terms reviews
independently.

An internal symbol-timeline store records exact, case-sensitive exchange/symbol
mappings as versioned instrument records. Each version contains up to 256
ordered, non-overlapping date intervals; starts are inclusive and ends
exclusive. Lookup takes both an effective date and `as_of`, returns the
instrument ID and immutable record ID, and fails when the timeline or date is
uncovered. Corrections append a new timeline and do not alter earlier as-of
results. These mappings are unassessed source assertions, not a verified
instrument master; no symbols are seeded, and CSV imports and trading do not
use them. A future ingestion path must review source authority and terms,
handle competing sources, and independently validate instrument identities.
An internal comparison can inspect up to 32 explicitly selected sources for one
symbol and effective date. It reads each source's latest version available at
the requested `as_of` from one database snapshot, reports missing mappings and
disagreement independently, and returns retained record IDs. It does not find
unselected sources, rank authority, verify source identity or resolve a
conflict; agreement alone is not validation. This diagnostic is not connected
to import normalization or trading. To inspect retained synthetic or
owner-supplied timelines:

```sh
uv run --locked bq data inspect-symbol --exchange-code YOUR_EXCHANGE \
  --symbol YOUR_SYMBOL --effective-on 2026-11-20 \
  --as-of 2026-11-22T12:00:00Z --source-id SOURCE_A --source-id SOURCE_B
```

The JSON response covers only the requested sources. This command uses local OS
file permissions rather than the web password.

Stored data versions can also be retrieved by their immutable record ID only
when their knowledge and record times are no later than `as_of`. An internal
adapter can turn such a version into a context item with stable source/version
identities and its quality label; it does not include the source URL in the
rendered content. Payload hashes detect payload corruption, not metadata
tampering or a complete database rewrite. Record times, source details, quality
labels and context classification remain assertions, and the general context
builder still accepts caller-created items. A future gateway must enforce
authoritative source selection rather than assuming this adapter is mandatory.
An internal replay helper can reconstruct the included prompt entries of a
stored manifest from retained data-record versions at its original `as_of`;
it refuses unsupported source types, missing versions and mismatched content
or provenance. It verifies only included entries, not dropped inputs; system
text, schema, classification, source authority and local timestamps remain
assertions. It is not a gateway, privacy approval or a defense against a
complete database rewrite.

The offline EOD CSV importer accepts owner-supplied UTF-8 data with the
header `instrument_id,trading_date,event_at,open,high,low,close,volume`.
It requires an explicit source, owner-reviewed terms URL and review timestamp,
currency, adjustment mode and idempotency key. Rows are validated and inserted
atomically, retain decimal prices as strings, and remain `UNASSESSED`. The
source and terms URLs must be HTTPS without credentials, queries or fragments.
The owner's review is an attestation, not a legal or data-quality verification;
only import material you are permitted to use. Original CSV bytes are not
retained, only a request hash, so preserve the source file separately. There is
no CSV API, instrument-master mapping, or completeness,
corporate-action or exchange-session verification.

An internal EOD diagnostic can inspect up to 366 caller-supplied expected dates
and exact retained record IDs for one source, instrument and adjustment mode.
It reports missing dates, the stored quality label and closing-price changes
strictly above a caller-supplied basis-point threshold; it does not compare
across a missing date. Records must have been known and recorded by `as_of`,
match the requested series and use one currency unit. This is not a quality
approval or readiness gate: dates, record selection and thresholds are caller
choices, older versions may be selected, and no calendar, source authority or
corporate action is verified. Outliers are indicators, not proof of errors.
The diagnostic does not change stored quality labels.

Alternatively, `inspect-eod` can derive expected dates from up to 31 inclusive
calendar dates when the owner explicitly asserts that every open session in the
range should have a bar for the chosen instrument. It refuses uncovered dates,
open sessions that have not closed by `as_of`, and ranges without open sessions.
The output includes each calendar date's retained session ID and state, bars
found in the import on recorded closed dates, and the quality report. For example:

```sh
uv run --locked bq data inspect-eod IMPORT_ID --source-id YOUR_SOURCE_ID \
  --instrument-id YOUR_INSTRUMENT_ID --adjustment UNADJUSTED \
  --calendar-exchange YOUR_EXCHANGE --calendar-start 2026-11-17 \
  --calendar-end 2026-11-19 --assume-open-sessions-expected \
  --as-of 2026-11-22T12:00:00Z --max-change-bps 10000
```

The exchange-to-instrument relationship and expected bars are owner
assertions, not verified listing, suspension or special-session rules. Calendar
and data are read in separate snapshots; `as_of` and locally recorded times
depend on the local clock. Closed-day bars are indicators, not verified errors;
only the selected import and requested instrument are inspected. The diagnostic
does not establish completeness, source authority or trading readiness.

To inspect one import, supply its displayed import ID, expected dates and
threshold explicitly:

```sh
uv run --locked bq data inspect-eod IMPORT_ID --source-id YOUR_SOURCE_ID \
  --instrument-id YOUR_INSTRUMENT_ID --adjustment UNADJUSTED \
  --date 2026-11-19 --date 2026-11-20 \
  --as-of 2026-11-21T00:00:00Z --max-change-bps 1000
```

Replace the example dates and timestamp with the actual expected dates and a
time no earlier than the import. The result is JSON for this import only;
other imports do not fill its missing dates. A `null` outlier value means no
comparison was possible, not that the data passed. This CLI uses the local OS
user's file permissions and does not prompt for the web password.

After sign-in, the data-status panel shows historical CSV import and imported
record counts plus up to 20 recent imports with source identifiers and UTC
timestamps. These counts do not indicate unique instruments or readiness for
trading. The endpoint `/api/v1/data/status` requires an owner session and does
not expose imported rows.

After confirming you are permitted to use a source, import a local file with:

```sh
uv run --locked bq data import-eod-csv path/to/file.csv \
  --import-key YOUR_UNIQUE_IMPORT_KEY --source-id YOUR_SOURCE_ID \
  --source-url https://example.test/data --review-url https://example.test/terms \
  --reviewed-at 2026-01-01T00:00:00Z --currency INR \
  --adjustment UNADJUSTED --confirm-terms-review
```

Replace the example URLs and review time with the actual reviewed source and
terms. Reuse an import key only for an identical request; a changed file or
metadata requires a new key. The command operates with the local OS user's
file access and does not prompt for the web password.

The job-run foundation records one durable occurrence per job key, supports
bounded retries and worker leases, and retains append-only transition events.
Completion requires the current per-claim token, preventing an expired attempt
from completing after its lease is reclaimed. Lease checks use the clock after
the database write lock is acquired. Job handlers will still need idempotent
side effects keyed by the occurrence.

Daily and interval schedule definitions can be saved as immutable, audited snapshots with
optimistic version checks. Validation checks the operating window, timezone,
dependencies and a caller-supplied catalogue of implemented jobs. Preview
returns UTC occurrences; an interval starts at the configured local time and
repeats every 1–1,440 whole minutes up to and including the operating-window
end, restarting at that local time each day. It does not apply market calendars,
dependencies or wake policies, and rejects ambiguous or nonexistent local times.
Schedules can explicitly select weekdays, numbered Monday `0` through Sunday `6`;
the default is every day, including holidays. A weekday selection is not a
trading-day rule. New snapshots
have an effective time no earlier than their record time; effective times must
advance. Selecting the snapshot for a time fails closed if no applicable
boundary exists or a later historical snapshot has no boundary. Existing
snapshots created before the boundary migration stay readable but cannot be
assumed effective; saving a new snapshot starts a new known boundary. Future
effective snapshots cannot be superseded by an earlier boundary.

A separate wake-policy decision evaluates an actual scheduled occurrence: the local operating
window always applies, an on-time wake is eligible, `SKIP` excludes late wakes,
`RUN_ON_WAKE_WITHIN` includes its configured cutoff, and `ALWAYS_RUN` can
consider an older occurrence. The internal store can persist an idempotent
decision for an explicitly supplied occurrence using the schedule version
effective at its scheduled time and a clock read after taking the database write
lock. A bounded internal batch operation can also discover and record daily and interval
occurrences in a caller-supplied inclusive range. It accepts at most 31 days,
1,000 occurrences, 256 effective-version changes and 10,000 enabled-schedule
checks; exceeding a limit or encountering an uncovered boundary, ambiguous
local time or audit error fails the batch without partial writes. Repeated
overlapping requests return the original decisions.
A `RUN` decision means only that the wake policy permits consideration;
it does not enqueue a job, check dependencies or establish catch-up safety.
The batch observation time is captured once before enumeration and must not
be used as a dispatch authorization. Neither wake processing nor
missed-occurrence discovery is automatic. The local
clock and append-only database are not independently trusted; pre-commit times
are not trusted commit checkpoints. No jobs are registered or started; no
automatic schedule trigger or session-event schedules are implemented.

After sign-in, the read-only schedule-status panel shows the effective version,
latest version, up to 20 schedules and 20 recent wake decisions with counts.
For each enabled daily or interval schedule it previews the next five occurrences after the
displayed observation time, assuming the current version stays unchanged.
Preview is withheld when a later version is pending, or when a local time is
ambiguous or nonexistent. It does not apply market calendars, dependencies or
wake policies and is not a promise that a job will run.
The authenticated `/api/v1/schedules/status` endpoint is not cached; it uses
one database snapshot and reports an unknown effective state when a boundary
is missing or activation is pending. The panel cannot create, edit or run
schedules. A wake-policy-eligible decision is not a dispatched job.

An internal context builder orders supplied source versions, excludes items
known or recorded after `as_of`, enforces section limits and required sections,
and accounts for the complete rendered prompt, declared overhead and reserved
output against a caller-supplied token counter. It deterministically drops only
optional items, or fails if the required context does not fit. It records
included and dropped identities, classifications and hashes in a manifest.
The system prompt, output schema and every source item require an explicit
classification. The manifest store persists the exact rendered prompt in an
append-only SQLite table, and verifies its hash on retrieval. It limits each
build to 256 items and 1 MiB of input.

These are internal foundations only: the timestamps and tokenizer must be
supplied by a trusted caller, and the token counter has not been validated for
any model. The saved prompt includes content in plaintext in the local database;
do not use sensitive content until storage and privacy controls are reviewed.
Hash checks do not protect against a complete database rewrite. Dropped content
is not retained, so the exact prompt can be recovered but a full rebuild from
source versions is not yet guaranteed. No prompt-injection protection,
model gateway or provider is implemented.

An internal eligibility function also checks task class, reviewed capabilities
and terms, approved data classifications, and local evaluations tied to a
task version and model fingerprint. It rejects private content on every free
tier and never chooses a local model for reasoning or multimodal tasks. These
checks currently rely entirely on caller-supplied classifications and review
metadata: they are not yet wired to trusted content classification, a task
registry or a model gateway.

An offline routing helper reads a persisted manifest, checks that the task,
model, included provenance and classifications match the rendered prompt, and
applies that eligibility policy. It excludes dropped items from the rendered
input classification, and rejects older manifests without the required
classifications. Trying a different model requires rebuilding the context for
that model; a prompt budgeted for one model is not silently reused for another.
This helper cannot establish that caller-supplied classifications or review
claims are true and is not a dispatch or privacy enforcement boundary. Dropped
private metadata can remain in the manifest; a future gateway must not send
the manifest wholesale to a provider.

An internal, append-only task-definition registry stores explicitly versioned
task policy, prompt and schema text and classifications, and prompt/spec version
identifiers, section limits and required sections. Changes require a new version
and produce an audit event; saving an identical definition is idempotent. A
separate routing helper requires an exact registered task version, compares
the rendered system, schema and their classifications, and checks section
requirements against the rendered content before applying eligibility checks.
No tasks are seeded.
The registry is writable by local code with database access, not authenticated
through the web owner session. It does not validate JSON Schema semantics,
enforce source selection or tokenizer accuracy, establish source classifications
or provider approvals, or define timeouts and fallback policy. Its hashes and audit events
cannot protect against a complete database rewrite. Task prompts and schemas
are stored in plaintext. Direct low-level routing remains available; any future
gateway must exclusively enforce registered policy and independently complete
the privacy and source-verification gates.

An internal offline model-output validator checks a task's existing schema at
use time against JSON Schema Draft 2020-12, parses at most 1 MiB of UTF-8 JSON,
rejects duplicate keys and non-finite numbers, and validates the result with
local references only. Unresolvable references fail without network access.
Schemas must be trusted: validation has no execution-time or regex complexity
limit. Any `$schema` field, including inside literal data, must identify Draft
2020-12; `format` remains an annotation, not an assertion. Numeric schema
constraints use exact decimal arithmetic. JSON number literals are limited to
512 characters, with decimal adjusted exponents between -308 and 308.
Fractional results are returned as `Decimal`, which requires an explicit
serialization policy; integral results are integers. Financial values still
require domain-specific unit, precision and range checks. Errors omit model
content. Schema validation does not establish
factual correctness, citation validity, privacy or safety. No gateway currently
calls this validator, and it does not repair or re-ask for invalid output.
An offline verification helper composes registered-task routing checks,
record-backed prompt replay and output-schema validation. It does not dispatch
requests, prove a response came from the selected model, establish source
authority or privacy, or replace a gateway and its independent safety gates.

For record-backed contexts, a task definition may specify a citation path as
an ordered tuple of object keys. When present, offline registered-output
verification extracts a nonempty, bounded array at that path. Each entry must
contain exactly `source_id` and `version_id`, and each distinct pair must be
included in the replayed prompt. Missing, malformed, duplicate and unknown
references fail. Definitions without a citation path retain their previous
behavior and do not receive citation checks. The task schema and prompt must
define what the citations cover; this check does not find citations elsewhere,
check claim entailment, verify source authority or prove every claim is cited.
No gateway currently invokes it.

The local attempt ledger records an intent before a future call and an
append-only outcome afterward. Missing or partial token usage remains unknown;
estimates are labeled, and a no-call cache hit records zero provider tokens.
Unfinished attempts remain visible after restart and are not automatically
retried. Each intent must match its manifest's task version and model ID.
Internal task/version/model summaries keep reported and estimated totals
separate and count missing input and output usage independently; incomplete
attempts are counted as unknown, not as zero. Run `uv run --locked bq llm usage`
to inspect these summaries as JSON; this command uses the local OS user's
permissions and does not prompt for the web password. The signed-in
model-usage panel shows the first 20 task/version/model groups and their
reported, estimated and unknown counts. The authenticated endpoint encodes
token totals as decimal strings to preserve integer precision.
No actual provider, cache or dispatch path currently uses the ledger,
so no-call claims and provider usage have not been independently verified.

The foundation provides nonnegative, integer-only paise and whole-share
quantity values. Zero is valid for an empty balance or position; future order
commands must separately require a positive quantity. No accounting or order
execution is implemented.

The locked dependency and license inventory is in
[THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md). It is not a legal review or
a redistribution bundle; the project has no declared redistribution license.

## News research agent

The first agent ingests market news from **Pulse by Zerodha**
(`https://pulse.zerodha.com/feed.xml`) and keeps what matters over the long
term. It makes **no model calls**: fetching, parsing, deduplication and
classification are deterministic, which is the cost strategy.

```sh
uv run --locked bq news ingest              # fetch, store new items, update memory
uv run --locked bq news digest --limit 20   # long-term-impact memories as JSON
```

Or `just news` from the repository root.

**Why it stays cheap.** Pulse's feed already carries a title and a one-line
summary for every story, so **full articles are never fetched**. A single
request returns roughly 150 stories in about 107 KB. Only that metadata is
stored (bounded to 512 characters for a title and 1,024 for a summary).

**Deduplication** uses no LLM: each item is keyed by a hash of its normalised
HTTPS URL plus an order-insensitive fingerprint of its title words. Re-ingesting
the same feed stores nothing and reports every item as a duplicate.

**Classification** uses whole-word keyword rules into eight categories:
`INDIA_MARKET`, `GLOBAL_MARKET`, `GEOPOLITICAL`, `COMMODITY`, `RATES`,
`CORPORATE`, `REGULATORY` and `OTHER`. Whole-word matching matters — substring
matching classified "celebrated" as a rate story.

**Memory** keeps only long-term-impact items. Geopolitical and regulatory news
qualifies outright; rate news must contain a structural word (repo, policy rate,
rate cut or hike, inflation) so routine RBI notices do not flood memory; other
categories need an explicit marker. Memories carry `known_at` and `expires_at`,
are filtered point-in-time, and track `hit_count` for later frecency pruning.
A live run produced 15 memories from 156 stories.

`news_items` and `agent_memories` are immutable by trigger. The authenticated
`GET /api/v1/news/digest` endpoint returns the 20 most recent items and
memories; it is owner-only and non-cacheable.

Limitations: classification is keyword-based and can mislabel a headline that
mentions two topics (a merger story that opens with "RBI" lands first-match).
`OTHER` is currently a large bucket. This agent performs no summarisation and
does not fetch article bodies; any model-assisted pass would need an explicit
token budget.

## Continuous integration

The project-level [CI workflow](.github/workflows/ci.yml) runs the
frontend install/build/audit, Python lint/type/tests, lock check, and wheel
asset and migration verification on Ubuntu and an Apple Silicon macOS runner. It checks
idempotent initialization and the native architecture on macOS. Its GitHub
Actions are pinned to commit SHAs. Hosted macOS CI may incur account charges
and does not replace the smoke test on the owner's Mac.
