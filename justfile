# BharatQuant task runner.
#
# Why `PYTHONPATH` is exported below: on macOS, Python's `site.py` skips any
# `.pth` file that carries the hidden file flag (`st_flags & UF_HIDDEN`). uv's
# editable-install `.pth` can end up with that flag, which makes `import bq`
# fail with "No module named 'bq'" even though the package is installed.
# Pointing PYTHONPATH at the source tree makes every command immune to it.
export PYTHONPATH := justfile_directory() / "bharatquant" / "backend" / "src"

default:
    @just --list

install:
    cd bharatquant && uv sync --locked --python 3.13
    cd bharatquant/web && npm ci

build:
    cd bharatquant/web && npm run build

init:
    cd bharatquant && uv run --locked bq init

auth-setup:
    cd bharatquant && uv run --locked bq auth setup

doctor:
    cd bharatquant && uv run --locked bq doctor

serve:
    cd bharatquant && uv run --locked bq serve

bench:
    cd bharatquant && uv run --locked bq bench

# Verified backup: just backup ~/Backups/bq-2026-11-26.db
backup output:
    cd bharatquant && uv run --locked bq db backup --output {{output}}

# Restore is destructive: just restore ~/Backups/bq-2026-11-26.db
restore backup:
    cd bharatquant && uv run --locked bq db restore --backup {{backup}} --confirm

run: build init doctor
    cd bharatquant && uv run --locked bq serve

web-dev:
    cd bharatquant/web && npm run dev

news:
    cd bharatquant && uv run --locked bq news ingest

# Analyse one Indian equity and print the structured run as JSON.
# Example: just analyze NSE:RELIANCE --equity-paise 100000000 --cash-paise 50000000
analyze symbol *ARGS:
    cd bharatquant && uv run --locked bq analyze {{symbol}} {{ARGS}}

# Show which market-data vendor serves each domain (Kite disabled by default).
vendors:
    cd bharatquant && uv run --locked bq data vendors

# Gate a trade-plan JSON through the risk engine; paper order only.
# Example: just paper-execute plan.json --account-id paper-main ...
paper-execute plan *ARGS:
    cd bharatquant && uv run --locked bq paper execute {{plan}} {{ARGS}}

test:
    cd bharatquant && uv run --locked pytest -q
    cd bharatquant/web && npm test

lint:
    cd bharatquant && uv run --locked ruff check .

format-check:
    cd bharatquant && uv run --locked ruff format --check .

typecheck:
    cd bharatquant/web && npm run typecheck
    cd bharatquant && uv run --locked mypy

check: lint format-check typecheck test

# ---- catalyst campaign ----
# First run: creates paper-main, saves defaults and seeds the daily schedules.
campaign-init:
    cd bharatquant && uv run --locked bq campaign init

campaign-refresh:
    cd bharatquant && uv run --locked bq campaign refresh-master

campaign-research:
    cd bharatquant && uv run --locked bq campaign research

campaign-scan:
    cd bharatquant && uv run --locked bq campaign scan

campaign-status:
    cd bharatquant && uv run --locked bq campaign status

campaign-review:
    cd bharatquant && uv run --locked bq campaign review
