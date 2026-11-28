"""``/api/v1/research/*`` end to end with a stubbed model turn."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"
DAYS = [datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index) for index in range(30)]
PATTERN = (
    [10000, 10020, 9980] * 4 + [11000] + [10900 - index * 180 for index in range(17)]
)
BARS = [
    {
        "symbol": "AAA",
        "ts": day.isoformat(),
        "open": price,
        "high": price + 60,
        "low": price - 60,
        "close": price,
        "volume": 5000,
    }
    for day, price in zip(DAYS, PATTERN, strict=True)
]
STRATEGY = {
    "strategy_id": "probe",
    "version": "1",
    "name": "probe",
    "kind": "RULE",
    "timeframe": "swing",
    "universe": ["AAA"],
    "entry_rule": "SMA_CROSS",
    "exit_rule": "SMA_CROSS_DOWN",
    "lookback": 3,
    "atr_period": 3,
    "stop_atr_multiple": 6.0,
    "target_atr_multiple": 12.0,
}


def _turn():
    calls = {"n": 0}

    def turn(prompt, observations):
        calls["n"] += 1
        if not observations:
            return {
                "action": "tool",
                "tool": "backtest",
                "arguments": {
                    "strategy": STRATEGY,
                    "bars": BARS,
                    "start": DAYS[0].isoformat(),
                    "end": DAYS[-1].isoformat(),
                    "initial_capital_paise": 1_000_000,
                },
            }
        evidence = observations[-1].get("evidence") or {}
        return {
            "action": "final",
            "text": "The backtest completed on the frozen slice.",
            "claims": [
                {
                    "run_id": observations[-1]["run_id"],
                    "metric_key": "return_bps",
                    "value": evidence["return_bps"],
                    "unit": "bp",
                    "time_basis": "point",
                }
            ],
        }

    return turn


@pytest.fixture()
def client(tmp_path: Path):
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    app = create_app(database_path=path, research_model_turn=lambda: _turn())
    with TestClient(app, base_url=ALLOWED_ORIGIN) as test_client:
        login = test_client.post(
            "/api/v1/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ALLOWED_ORIGIN},
        )
        assert login.status_code == 200
        test_client.headers.update(
            {
                "Origin": ALLOWED_ORIGIN,
                "X-CSRF-Token": login.json()["csrf_token"],
            }
        )
        yield test_client


def _wait_for(client: TestClient, run_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/research/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "blocked", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("run did not reach a terminal state")


def test_reads_require_a_session(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    app = create_app(database_path=path, research_model_turn=lambda: _turn())
    with TestClient(app, base_url=ALLOWED_ORIGIN) as anonymous:
        assert anonymous.get("/api/v1/research/sessions").status_code == 401


def test_writes_require_csrf(client: TestClient) -> None:
    response = client.post(
        "/api/v1/research/sessions",
        json={"title": "no csrf"},
        headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": "wrong"},
    )
    assert response.status_code == 403


def test_session_to_grounded_run_to_stream(client: TestClient) -> None:
    created = client.post("/api/v1/research/sessions", json={"title": "momentum"})
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    sent = client.post(
        f"/api/v1/research/sessions/{session_id}/messages",
        json={"prompt": "backtest the probe strategy"},
    )
    assert sent.status_code == 202
    run_id = sent.json()["run_id"]

    detail = _wait_for(client, run_id)
    assert detail["status"] == "completed"
    assert detail["validation"]["passed"] is True
    assert detail["metrics"]["tools_used"] == ["backtest"]
    assert detail["metrics_json"] if "metrics_json" in detail else True
    assert detail["data_manifest_sha256"] is None
    evidence = detail["evidence"]
    assert any(item["metric_key"] == "return_bps" for item in evidence)
    assert detail["risk_xray.json"]["episodes"]

    listed = client.get("/api/v1/research/runs", params={"session_id": session_id})
    assert listed.status_code == 200
    assert len(listed.json()["runs"]) == 1

    stream = client.get(
        f"/api/v1/research/sessions/{session_id}/events", params={"run_id": run_id}
    )
    assert stream.status_code == 200
    assert "text/event-stream" in stream.headers["content-type"]
    kinds = [
        line
        for line in stream.text.splitlines()
        if line.startswith("data:") and '"type": "evidence_verdict"' in line
    ]
    assert kinds

    exported = client.get(f"/api/v1/research/runs/{run_id}/export.csv")
    assert exported.status_code == 200
    assert exported.text.startswith("symbol,entry_at,exit_at")


def test_cancel_without_a_running_run_is_conflict(client: TestClient) -> None:
    session_id = client.post(
        "/api/v1/research/sessions", json={"title": "idle"}
    ).json()["session_id"]
    response = client.post(f"/api/v1/research/sessions/{session_id}/cancel")
    assert response.status_code == 409


def test_artifacts_create_revise_search_diff_export(client: TestClient) -> None:
    first = client.post(
        "/api/v1/research/artifacts",
        json={"slug": "momentum", "title": "v1", "body_md": "IC 0.03 at 5d"},
    )
    assert first.status_code == 201
    second = client.post(
        "/api/v1/research/artifacts",
        json={"slug": "momentum", "title": "v2", "body_md": "IC 0.04 after costs"},
    )
    assert second.status_code == 201
    artifact_id = second.json()["artifact_id"]
    assert second.json()["revision"] == 2
    assert second.json()["parent_revision"] == 1

    found = client.get("/api/v1/research/artifacts", params={"q": "costs"})
    assert found.status_code == 200
    assert found.json()["artifacts"][0]["slug"] == "momentum"

    diff = client.get(
        f"/api/v1/research/artifacts/{artifact_id}/diff",
        params={"rev_a": 1, "rev_b": 2},
    )
    assert diff.status_code == 200
    assert diff.json()["title_changed"] is True

    as_md = client.get(
        f"/api/v1/research/artifacts/{artifact_id}/export", params={"format": "md"}
    )
    assert as_md.status_code == 200
    assert as_md.text.startswith("# v2")


def test_models_round_trip(client: TestClient) -> None:
    read = client.get("/api/v1/research/models")
    assert read.status_code == 200
    payload = read.json()
    assert payload["research_lab"]["planner"]["tier"] == "reasoning"
    assert payload["routes"]["research_lab_planner"]["tier"] == "reasoning"

    payload["research_lab"]["planner"]["tier"] = "flash"
    payload["research_lab"]["budgets"]["max_iterations"] = 7
    written = client.put(
        "/api/v1/research/models", json={"research_lab": payload["research_lab"]}
    )
    assert written.status_code == 200
    assert written.json()["research_lab"]["planner"]["tier"] == "flash"
    assert written.json()["research_lab"]["budgets"]["max_iterations"] == 7

    bad = client.put(
        "/api/v1/research/models",
        json={"research_lab": {"planner": {"tier": "decision"}}},
    )
    assert bad.status_code == 422


def test_alpha_bench_and_reconcile(client: TestClient) -> None:
    factor = [
        {
            "symbol": f"S{index:02d}",
            "ts": day.isoformat(),
            "factor_value": float(index + 1),
        }
        for index in range(6)
        for day in DAYS[:6]
    ]
    bars = [
        {
            "symbol": f"S{index:02d}",
            "ts": day.isoformat(),
            "close": 10000 + index * 50 + position * (index + 1) * 10,
        }
        for index in range(6)
        for position, day in enumerate(DAYS[:6])
    ]
    bench = client.post(
        "/api/v1/research/alpha/bench",
        json={"bars": bars, "factor": factor, "horizons": [1]},
    )
    assert bench.status_code == 200
    assert bench.json()["_meta"]["survivorship_bias_warning"] is True

    reconcile = client.post(
        "/api/v1/research/paper/reconcile",
        json={
            "initial_cash_paise": 2_000_000,
            "fills": [
                {
                    "symbol": "AAA",
                    "side": "BUY",
                    "quantity": 100,
                    "price_paise": 10000,
                    "fees_paise": 1187,
                }
            ],
            "ending_prices": {"AAA": 10500},
            "reported_state": {
                "cash_paise": 998813,
                "fees_paise": 1187,
                "realized_pnl_paise": 0,
                "unrealized_pnl_paise": 50000,
                "nav_paise": 2048813,
            },
        },
    )
    assert reconcile.status_code == 200
    assert reconcile.json()["passed"] is True
