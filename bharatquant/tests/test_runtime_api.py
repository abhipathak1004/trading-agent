from pathlib import Path

from fastapi.testclient import TestClient

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate

PASSWORD = "synthetic owner password"


def test_runtime_status_metrics_and_event_socket(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    app = create_app(database_path=path)
    with TestClient(app, base_url=ALLOWED_ORIGIN) as client:
        assert client.get("/api/v1/system/status").status_code == 401
        login = client.post(
            "/api/v1/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ALLOWED_ORIGIN},
        )
        assert login.status_code == 200
        cookie = client.cookies.get("bq_session")
        assert cookie is not None

        status = client.get("/api/v1/system/status")
        assert status.status_code == 200
        assert status.json()["integrity"] == "ok"
        assert status.headers["cache-control"] == "no-store"

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "bq_runtime_degraded 0" in metrics.text
        assert "text/plain" in metrics.headers["content-type"]

        with client.websocket_connect(
            "/api/v1/events", cookies={"bq_session": cookie}
        ) as websocket:
            event = websocket.receive_json()
            assert event["type"] == "runtime"
            assert event["data"]["status"] == "ok"
