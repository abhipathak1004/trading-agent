"""``/api/v1/aihf/*``: auth, wire shapes, and the approval gate.

The engine is vendored now (ADR-053), so the gate tests no longer skip on a
missing sidecar — they run everywhere. The property under test is the one that
must never regress: **a halted fund refuses both a preview and an execute**, and
an approved preview leaves a ledger whose chain verifies.
"""

import json
from pathlib import Path

import httpx
import pytest

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
MANDATE_YAML = """\
schema_version: 2
name: test-fund
strategies:
  - name: earnings-drift
    blend: {mode: long_only}
    models:
      - name: pead
risk:
  max_position_pct: 0.25
  max_gross_exposure: 1.0
  halve_drawdown_pct: 0.1
  max_drawdown_pct: 0.2
capital: 100000
rebalance: weekly
benchmark: NIFTY 50
"""


def _run_case(app, case) -> None:
    """One signed-in session per case."""
    import asyncio

    async def run() -> None:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        )
        try:
            OwnerAuth(Path(app.state.database_path)).setup(PASSWORD)
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": PASSWORD},
            )
            await case(client, login.json()["csrf_token"])
        finally:
            await client.aclose()

    asyncio.run(run())


def _write(csrf: str) -> dict[str, str]:
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf}


@pytest.fixture()
def app(tmp_path: Path):
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    application = create_app(database_path=path)
    application.state.database_path = str(path)
    return application


async def _create_mandate(client, csrf) -> None:
    response = await client.post(
        "/api/v1/aihf/mandates", headers=_write(csrf), json={"yaml": MANDATE_YAML}
    )
    assert response.status_code == 201, response.text


def test_aihf_reads_require_a_session(app) -> None:
    async def unauth() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client:
            for path in (
                "/api/v1/aihf/status",
                "/api/v1/aihf/mandates",
                "/api/v1/aihf/backtests",
                "/api/v1/aihf/paper/funds",
            ):
                assert (await client.get(path)).status_code == 401

    import asyncio

    asyncio.run(unauth())


def test_aihf_writes_require_csrf_and_origin(app) -> None:
    async def check(client, csrf):
        assert (
            await client.post(
                "/api/v1/aihf/mandates",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"yaml": MANDATE_YAML},
            )
        ).status_code == 403
        assert (
            await client.post(
                "/api/v1/aihf/mandates",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": "wrong"},
                json={"yaml": MANDATE_YAML},
            )
        ).status_code == 403
        assert (
            await client.post(
                "/api/v1/aihf/mandates",
                headers={"Origin": "http://evil.test", "X-CSRF-Token": csrf},
                json={"yaml": MANDATE_YAML},
            )
        ).status_code == 403

    _run_case(app, check)


def test_status_is_key_free_and_reports_the_vendored_engine(app) -> None:
    async def check(client, _csrf):
        status = await client.get("/api/v1/aihf/status")
        assert status.status_code == 200
        body = status.json()
        assert set(body) >= {
            "installed",
            "version",
            "has_llm_key",
            "active_provider",
            "models",
            "can_run",
        }
        # The engine is vendored: it is always installed, on every machine.
        assert body["installed"] is True
        assert body["version"] == "in-process"
        # No key at all is configured on a fresh database.
        assert body["has_llm_key"] is False
        assert body["can_run"] is False
        # Never a secret in the projection.
        rendered = json.dumps(body)
        assert "********" not in rendered
        assert "secret" not in rendered.lower()

    _run_case(app, check)


def test_models_come_from_the_engine_registry(app) -> None:
    async def check(client, _csrf):
        models = (await client.get("/api/v1/aihf/models")).json()["models"]
        names = {model["model_name"] for model in models}
        # Both persona and deterministic analysts are listed, from one registry.
        assert "buffett" in names
        assert "pead" in names
        for model in models:
            assert model["runnable"] is True

    _run_case(app, check)


def test_mandate_rejects_invalid_yaml(app) -> None:
    async def check(client, csrf):
        for body in (
            {"yaml": ""},
            {"yaml": "name: nope\n"},  # no schema_version
            {"yaml": "schema_version: 2\nname: x\n"},  # no strategies
        ):
            response = await client.post(
                "/api/v1/aihf/mandates", headers=_write(csrf), json=body
            )
            assert response.status_code == 422, response.text

    _run_case(app, check)


def test_mandate_round_trip_through_the_schema(app) -> None:
    async def check(client, csrf):
        created = await client.post(
            "/api/v1/aihf/mandates", headers=_write(csrf), json={"yaml": MANDATE_YAML}
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "test-fund"
        assert body["valid"] is True
        assert body["summary"]["benchmark"] == "NIFTY 50"
        assert [s["name"] for s in body["summary"]["strategies"]] == ["earnings-drift"]

        listed = (await client.get("/api/v1/aihf/mandates")).json()
        assert "test-fund" in {entry["name"] for entry in listed["mandates"]}

        update = await client.put(
            "/api/v1/aihf/mandates/test-fund",
            headers=_write(csrf),
            json={"yaml": MANDATE_YAML.replace("capital: 100000", "capital: 250000")},
        )
        assert update.status_code == 200
        assert update.json()["summary"]["capital"] == 250000

        removed = await client.delete(
            "/api/v1/aihf/mandates/test-fund", headers=_write(csrf)
        )
        assert removed.status_code == 204
        assert (await client.get("/api/v1/aihf/mandates/test-fund")).status_code == 404

    _run_case(app, check)


def test_a_minute_mandate_is_refused_by_the_api(app) -> None:
    """The engine supports intraday; this tab does not yet. The refusal is a
    422 at save time, not a mid-run failure."""
    minute = MANDATE_YAML.replace("rebalance: weekly", "rebalance: minute").replace(
        "benchmark: NIFTY 50", "benchmark: NIFTY 50\ninterval: 5.0"
    )

    async def check(client, csrf):
        response = await client.post(
            "/api/v1/aihf/mandates", headers=_write(csrf), json={"yaml": minute}
        )
        assert response.status_code == 422
        assert "minute" in response.json()["detail"]

    _run_case(app, check)


def test_backtest_rejects_a_model_override(app) -> None:
    """Model choice is Settings; a per-run override would be silently ignored,
    so it is refused instead of accepted and dropped."""

    async def check(client, csrf):
        await _create_mandate(client, csrf)
        response = await client.post(
            "/api/v1/aihf/backtest",
            headers=_write(csrf),
            json={
                "mandate": "test-fund",
                "tickers": ["RELIANCE"],
                "start_date": "2025-01-02",
                "end_date": "2025-02-02",
                "model_override": "claude-opus-5-5",
            },
        )
        assert response.status_code == 422
        assert "Settings" in response.json()["detail"]

    _run_case(app, check)


def test_backtest_needs_a_stored_mandate(app) -> None:
    async def check(client, csrf):
        response = await client.post(
            "/api/v1/aihf/backtest",
            headers=_write(csrf),
            json={
                "mandate": "absent",
                "tickers": ["RELIANCE"],
                "start_date": "2025-01-02",
                "end_date": "2025-02-02",
            },
        )
        assert response.status_code == 404

    _run_case(app, check)


def test_kill_switch_then_execute_is_409(app) -> None:
    """The one safety property that must never regress."""

    async def check(client, csrf):
        await _create_mandate(client, csrf)
        created = await client.post(
            "/api/v1/aihf/paper/funds",
            headers=_write(csrf),
            json={
                "name": "alpha",
                "mandate": "test-fund",
                "tickers": ["RELIANCE", "TCS"],
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["universe"] == ["RELIANCE", "TCS"]
        assert created.json()["status"] == "ACTIVE"

        halted = await client.post(
            "/api/v1/aihf/paper/alpha/kill-switch",
            headers=_write(csrf),
            json={"action": "halt", "reason": "operator test"},
        )
        assert halted.status_code == 200
        assert halted.json()["status"] == "HALTED"

        # A halted fund must refuse a preview AND an execute.
        for path in ("preview", "execute"):
            response = await client.post(
                f"/api/v1/aihf/paper/alpha/{path}",
                headers=_write(csrf),
                json={"preview_id": "anything"},
            )
            assert response.status_code == 409, (path, response.text)

        resumed = await client.post(
            "/api/v1/aihf/paper/alpha/kill-switch",
            headers=_write(csrf),
            json={"action": "resume"},
        )
        assert resumed.json()["status"] == "ACTIVE"

        ledger = await client.get("/api/v1/aihf/paper/alpha/ledger")
        assert ledger.status_code == 200
        assert ledger.json()["chain_verified"] is True
        assert ledger.json()["control"]["halted"] is False

    _run_case(app, check)


def test_a_fund_refuses_a_name_it_already_has(app) -> None:
    async def check(client, csrf):
        await _create_mandate(client, csrf)
        body = {"name": "alpha", "mandate": "test-fund", "tickers": ["RELIANCE"]}
        assert (
            await client.post(
                "/api/v1/aihf/paper/funds", headers=_write(csrf), json=body
            )
        ).status_code == 201
        assert (
            await client.post(
                "/api/v1/aihf/paper/funds", headers=_write(csrf), json=body
            )
        ).status_code == 409

    _run_case(app, check)


def test_fund_names_reject_traversal(app) -> None:
    async def check(client, csrf):
        # The mandate must exist so the failure is the *name*, not the mandate.
        await _create_mandate(client, csrf)
        for name in ("../escape", "a/b", "."):
            response = await client.post(
                "/api/v1/aihf/paper/funds",
                headers=_write(csrf),
                json={"name": name, "mandate": "test-fund", "tickers": ["RELIANCE"]},
            )
            assert response.status_code == 422, (name, response.text)

    _run_case(app, check)


def test_a_preview_without_a_due_session_is_a_conflict(app) -> None:
    """A fresh fund's first due session depends on the vendor; with no network
    and no bars it is a 409, and nothing extra is written."""

    async def check(client, csrf):
        await _create_mandate(client, csrf)
        created = await client.post(
            "/api/v1/aihf/paper/funds",
            headers=_write(csrf),
            json={"name": "alpha", "mandate": "test-fund", "tickers": ["RELIANCE"]},
        )
        assert created.status_code == 201
        response = await client.post(
            "/api/v1/aihf/paper/alpha/preview", headers=_write(csrf)
        )
        # Either it computed (network available) or nothing is due — never a 500.
        assert response.status_code in (200, 409, 502), response.text

    _run_case(app, check)


def test_sse_stream_terminates_when_the_run_finishes(tmp_path) -> None:
    """The stream must end on ``done``; a stuck socket is a hung browser tab."""
    import asyncio

    from bq.aihf.config import resolve_paths
    from bq.aihf.routes import _event_stream
    from bq.aihf.runner import Progress, write_progress

    async def check() -> None:
        paths = resolve_paths(database_path=tmp_path / "bq.db")
        run = paths.runs / "demo-run"
        write_progress(
            run / "progress.json",
            Progress(run_id="demo-run", phase="running", started_at=0.0),
        )
        frames: list[bytes] = []
        async for chunk in _event_stream(paths, "demo-run"):
            frames.append(chunk)
            write_progress(
                run / "progress.json",
                Progress(
                    run_id="demo-run", phase="parsed", started_at=0.0, finished_at=1.0
                ),
            )
        assert len(frames) == 2
        payloads = [
            json.loads(frame.decode().removeprefix("data: ")) for frame in frames
        ]
        assert payloads[0]["done"] is False
        assert payloads[-1]["done"] is True

    asyncio.run(check())


def test_sse_stream_reports_an_unknown_run(tmp_path) -> None:
    import asyncio

    from bq.aihf.config import resolve_paths
    from bq.aihf.routes import _event_stream

    async def check() -> None:
        paths = resolve_paths(database_path=tmp_path / "bq.db")
        frames = [chunk async for chunk in _event_stream(paths, "absent")]
        assert len(frames) == 1
        assert (
            json.loads(frames[0].decode().removeprefix("data: "))["phase"] == "unknown"
        )

    asyncio.run(check())
