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

run: build init
    cd bharatquant && uv run --locked bq serve

web-dev:
    cd bharatquant/web && npm run dev

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
