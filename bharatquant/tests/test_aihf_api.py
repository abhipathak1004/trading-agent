"""``/api/v1/aihf/*``: auth, wire shapes, and the approval gate.

The gate tests run against the real aihf sidecar because the whole point is
that aihf's own ledger decides what is adoptable. They skip when the sidecar
is not provisioned, so the suite stays honest on a machine without it.
"""

import json
from pathlib import Path

import httpx
import pytest

from bq.aihf.config import resolve_paths
from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
MANDATE_YAML = """\
schema_version: 2
name: test-fund
strategies:
  - name: deep-value
    blend: {mode: long_only}
    models:
      - name: graham
        weight: 2.0
      - name: buffett
risk:
  max_position_pct: 0.25
  max_gross_exposure: 1.0
  halve_drawdown_pct: 0.1
  max_drawdown_pct: 0.2
capital: 100000
rebalance: weekly
benchmark: SPY
"""


def _sidecar() -> bool:
    return resolve_paths(database_path=Path("/tmp/probe/bq.db")).available


def _run(awaitable) -> None:
    import asyncio

    asyncio.run(awaitable)


def _run_case(app, case) -> None:
    """One signed-in session per case.

    httpx opens a client on its first request, so the client is created here
    and closed in ``finally`` rather than used as a context manager — the
    login request has already opened it.
    """
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
                "/api/v1/aihf/settings",
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
        # Authenticated but no CSRF token.
        assert (
            await client.post(
                "/api/v1/aihf/mandates",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": "wrong"},
                json={"yaml": MANDATE_YAML},
            )
        ).status_code == 403
        # CSRF but a foreign origin.
        assert (
            await client.post(
                "/api/v1/aihf/mandates",
                headers={"Origin": "http://evil.test", "X-CSRF-Token": csrf},
                json={"yaml": MANDATE_YAML},
            )
        ).status_code == 403

    _run_case(app, check)


def test_status_and_models_are_key_free(app) -> None:
    async def check(client, _csrf):
        status = await client.get("/api/v1/aihf/status")
        assert status.status_code == 200
        body = status.json()
        assert set(body) >= {
            "installed",
            "version",
            "has_llm_key",
            "has_financial_datasets_key",
            "active_provider",
            "active_model",
            "models",
            "can_run",
        }
        # The variables are named; their values must never appear.
        rendered = json.dumps(body)
        assert "********" not in rendered
        assert "secret" not in rendered.lower()
        assert body["has_financial_datasets_key"] is False
        assert body["has_llm_key"] is False
        assert body["can_run"] is False

        models = (await client.get("/api/v1/aihf/models")).json()["models"]
        assert all(isinstance(model["runnable"], bool) for model in models)

    _run_case(app, check)


def test_data_key_round_trips_masked(app) -> None:
    async def check(client, csrf):
        denied = await client.put(
            "/api/v1/aihf/settings",
            headers={"Origin": ALLOWED_ORIGIN},
            json={"financial_datasets_api_key": "fd-secret-key-1234"},
        )
        assert denied.status_code == 403

        saved = await client.put(
            "/api/v1/aihf/settings",
            headers=_write(csrf),
            json={"financial_datasets_api_key": "fd-secret-key-1234"},
        )
        assert saved.status_code == 200
        assert saved.json() == {
            "financial_datasets": {"configured": True, "masked": "********1234"}
        }
        assert "fd-secret" not in saved.text

        # The projection is the only shape the key leaves in.
        current = await client.get("/api/v1/aihf/settings")
        assert current.json() == saved.json()

        status = (await client.get("/api/v1/aihf/status")).json()
        assert status["has_financial_datasets_key"] is True

        cleared = await client.put(
            "/api/v1/aihf/settings",
            headers=_write(csrf),
            json={"financial_datasets_api_key": ""},
        )
        assert cleared.json() == {
            "financial_datasets": {"configured": False, "masked": None}
        }

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
            assert response.status_code in (422, 503), response.text

    _run_case(app, check)


@pytest.mark.skipif(not _sidecar(), reason="aihf sidecar not installed")
def test_mandate_round_trip_through_the_schema(app) -> None:
    async def check(client, csrf):
        created = await client.post(
            "/api/v1/aihf/mandates", headers=_write(csrf), json={"yaml": MANDATE_YAML}
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "test-fund"
        assert body["valid"] is True
        assert [s["title"] for s in body["summary"]["strategies"]] == ["Deep Value"]

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


@pytest.mark.skipif(not _sidecar(), reason="aihf sidecar not installed")
def test_backtest_refuses_without_credentials(app) -> None:
    """A run without keys is a clear 409, not a confusing subprocess failure."""

    async def check(client, csrf):
        await client.post(
            "/api/v1/aihf/mandates", headers=_write(csrf), json={"yaml": MANDATE_YAML}
        )
        response = await client.post(
            "/api/v1/aihf/backtest",
            headers=_write(csrf),
            json={
                "mandate": "test-fund",
                "tickers": ["AAPL"],
                "start_date": "2025-01-02",
                "end_date": "2025-02-02",
            },
        )
        assert response.status_code == 409
        assert "Settings" in response.json()["detail"]

    _run_case(app, check)


@pytest.mark.skipif(not _sidecar(), reason="aihf sidecar not installed")
def test_kill_switch_then_execute_is_409(app) -> None:
    """The one safety property that must never regress."""

    async def check(client, csrf):
        await client.post(
            "/api/v1/aihf/mandates", headers=_write(csrf), json={"yaml": MANDATE_YAML}
        )
        created = await client.post(
            "/api/v1/aihf/paper/funds",
            headers=_write(csrf),
            json={"name": "alpha", "mandate": "test-fund", "tickers": ["AAPL", "MSFT"]},
        )
        assert created.status_code == 201, created.text
        assert created.json()["universe"] == ["AAPL", "MSFT"]

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


def test_fund_names_reject_traversal(app) -> None:
    async def check(client, csrf):
        for name in ("../escape", "a/b", "."):
            response = await client.post(
                "/api/v1/aihf/paper/funds",
                headers=_write(csrf),
                json={"name": name, "mandate": "example", "tickers": ["AAPL"]},
            )
            assert response.status_code in (404, 422), (name, response.text)

    _run_case(app, check)


def test_sse_stream_terminates_when_the_run_finishes(tmp_path) -> None:
    """The stream must end on ``done``; a stuck socket is a hung browser tab."""
    import asyncio
    import json

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
    import json

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
