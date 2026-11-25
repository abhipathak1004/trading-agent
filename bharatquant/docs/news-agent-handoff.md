# BharatQuant — working handoff (compacted)

_Generated 2026-11-25 at the start of the news-research-agent task. Read this instead of the full chat history._

## What the project is

`/Users/abhishek/Documents/GitHub/trading-agent` — **BharatQuant**, a personal, paper-only Indian-equities research/trading app. Branch `dev`.

- **Backend**: Python 3.13, `bharatquant/backend/src/bq/` — stdlib `sqlite3` (WAL) + plain-SQL numbered migrations, FastAPI, no ORM.
- **Frontend**: `bharatquant/web/` — React 19 + TypeScript + Vite, dark violet theme, hand-written CSS in `src/styles.css`, self-hosted Inter + JetBrains Mono. Build outputs to `bharatquant/backend/src/bq/static/` (checked in).
- **Docs**: `DEVELOPMENT_STATUS.md` (status), `trading agent plan.md` (v3.12 plan, 3000+ lines), `BharatQuant UI-UX Development Guide v1.0.md`.
- **Task runner**: `justfile` — `just run` = build → init → doctor → serve.

## Established conventions (MUST follow)

- **Migrations**: numbered `NNN_name.sql` in `bq/infra/migrations/`, must be consecutive from 001. Use `STRICT` tables. Append-only tables get `no_update` / `no_delete` / `no_replace` triggers. Next free number is **022**.
- **Audit**: `bq.infra.audit.append_event(connection, occurred_at=..., actor="owner", action="domain.object.verb", object_type=..., object_id=..., payload={...})` inside the same transaction.
- **Stores**: `class XStore(path, clock=None)` with `_connect()` opening `file:...?mode=rw`, `BEGIN IMMEDIATE` + `with connection:` for writes.
- **Domain**: pure dataclasses (`frozen=True, slots=True`) with validation in `__post_init__`, `_utc()`/`_timestamp()` helpers, sha256 `_hash()` of canonical JSON.
- **API**: endpoints inside `create_app()` in `bq/api.py`; private routes check `auth.csrf_for_session(request.cookies.get(COOKIE_NAME)) is None → 401`; add path prefix to the `no-store` tuple in `security_headers`; mutations need `verify_origin`.
- **Errors**: subclass `ValueError` (`ResearchError`, `ScheduleError`, etc.).
- Tests in `bharatquant/tests/test_<domain>.py`, pytest, `FrozenClock` fixture class, httpx `ASGITransport` for API tests.

## Verified state

- Full suite: **303 pytest** passing; **13** frontend tests; ruff clean; typecheck clean.
- Last commit `3923fda feat: consolidate configuration into a Settings tab`.
- **Recurring snag**: `ModuleNotFoundError: No module named 'bq'` = stale `.venv` (a hidden `_editable_impl_bharatquant.pth` gets skipped). Fix: `rm -rf bharatquant/.venv && env -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/uv-cache uv sync --locked --python /opt/homebrew/bin/python3.13`. Always run python via `env -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/uv-cache uv run --locked ...`.
- **`node_modules` is tracked in git** (unfortunate but real). After any build, restore noise with `git checkout -- bharatquant/web/node_modules`.
- Build noise: stale duplicate `index N.html` / `font N.woff2` files regenerate; Vite `emptyOutDir` removes them — safe to commit the deletions.

## Existing relevant code

| Module | Purpose |
|---|---|
| `bq/research.py` | `ResearchStore`, `Filing`, `Thesis` — point-in-time research primitives (this is where news fits) |
| `bq/infra/database.py` | `migrate()`, `check_database()`, `APPLICATION_ID` |
| `bq/core.py` | `Clock`, `SystemClock`, `CurrencyPaise`, `Quantity`, `EXCHANGE_TIMEZONE` (Asia/Kolkata) |
| `bq/data/records.py` | `DataRecordStore` — PIT data records with `known_at` |
| `bq/data/providers.py` | `DataProvider` protocol |
| `bq/api.py` | All routes + `SettingsOverview` (shows tasks/schedules/agents state) |
| `bq/context.py` | `ContextBuilder`, `ContextSection` (has a `NOTES` section for news) |

## Current task (in progress)

**Build the first agent: a news research agent.**

Requirements from the user:
1. Browse internet / news about Indian markets, other markets, geopolitical news.
2. Use **pulse.zerodha.com** as the news source.
3. **Agent memory**: keep news with **long-term impact**; keep a **short/de-dup form of all news** for deduplication.
4. **Design for news volume** — be as **cost-efficient as possible**.

### Findings about the source (verified 2026-11-25)

- `https://pulse.zerodha.com/feed.xml` → **200**, ~110 KB RSS, **156 `<item>`** entries.
- Each item has only: `title`, `description` (1–2 line summary), `link`, `guid`, `pubDate` (RFC-822, `+0530`).
- **No full article fetch needed** — title + description is enough. This is the key cost lever.
- `pulse.zerodha.com/rss`, `/feed`, `/api/news` → 404. Only `/feed.xml` works. Single feed, no per-category feeds.

### Chosen design (cost-first)

- Parse RSS with stdlib `xml.etree.ElementTree` (no new dependency).
- **Dedupe for free, no LLM**: `url_hash` (sha256 of normalized link) + `shingle_hash` (sha256 of normalized, sorted title tokens) — catches republished/reworded dupes.
- **Deterministic tagging, no LLM**: keyword rules → categories (`INDIA_MARKET`, `GLOBAL_MARKET`, `GEOPOLITICAL`, `COMMODITY`, `RATES`, `CORPORATE`, `REGULATORY`, `OTHER`) and an impact hint.
- **Memory split**: every item gets a compact dedupe row; only items flagged `LONG_TERM_IMPACT` (deterministic rules) get a memory row. LLM summarisation only for high-impact items and only when explicitly enabled — off by default.
- Caps: max items per ingest, max title/description bytes, bounded feed size, https-only URL, timeout.
