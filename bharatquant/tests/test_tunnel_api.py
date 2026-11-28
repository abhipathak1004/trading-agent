import asyncio
from pathlib import Path

import httpx

from bq.api import ALLOWED_ORIGIN, create_app
from bq.auth import OwnerAuth
from bq.infra.database import migrate
from bq.infra.tunnel import TunnelManager, TunnelState

PASSWORD = "synthetic owner password"
TUNNEL_URL = "https://brave-unit-4821.trycloudflare.com"
TUNNEL_HOST = "brave-unit-4821.trycloudflare.com"


class FakeReader:
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        if not self._lines:
            return b""
        return (self._lines.pop(0) + "\n").encode("utf-8")


class FakeProcess:
    def __init__(self, lines: list[str]) -> None:
        self.returncode: int | None = None
        self.stderr = FakeReader(lines)
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.returncode = 0
        self._exited.set()

    def kill(self) -> None:
        self.returncode = -9
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


def _manager(tmp_path: Path) -> TunnelManager:
    async def spawn(argv: object) -> FakeProcess:
        del argv
        return FakeProcess(
            [
                f"INF Your quick Tunnel has been created: {TUNNEL_URL}",
                "INF Registered tunnel connection connIndex=0 protocol=quic",
            ]
        )

    async def healthy(url: str) -> bool:
        return True

    async def install(bin_dir: Path) -> Path:
        bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        binary = bin_dir / "cloudflared"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o700)
        return binary

    return TunnelManager(tmp_path, spawn=spawn, health_probe=healthy, installer=install)


def _app(tmp_path: Path) -> tuple[Path, object, TunnelManager]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    OwnerAuth(path).setup(PASSWORD)
    manager = _manager(tmp_path)
    app = create_app(database_path=path, tunnel_manager=manager)
    return path, app, manager


async def _signed_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200
    return {"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


async def _wait_for(done: object) -> None:  # type: ignore[no-untyped-def]
    for _ in range(500):
        if done():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("tunnel did not reach the expected state")


def test_tunnel_requires_auth_and_csrf(tmp_path: Path) -> None:
    _path, app, _manager_instance = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            assert (await client.get("/api/v1/settings/tunnel")).status_code == 401
            headers = await _signed_in(client)
            assert (
                await client.post(
                    "/api/v1/settings/tunnel/start",
                    headers={"Origin": ALLOWED_ORIGIN},
                )
            ).status_code == 403
            start = await client.post("/api/v1/settings/tunnel/start", headers=headers)
            assert start.status_code == 202
            assert start.headers["cache-control"] == "no-store"

    asyncio.run(check())


def test_tunnel_round_trip_and_host_gate(tmp_path: Path) -> None:
    _path, app, manager = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)

            initial = await client.get("/api/v1/settings/tunnel")
            assert initial.json()["state"] == "stopped"
            assert initial.json()["url"] is None

            assert (
                await client.post("/api/v1/settings/tunnel/start", headers=headers)
            ).status_code == 202
            await _wait_for(lambda: manager.snapshot().state is TunnelState.LIVE)

            live = await client.get("/api/v1/settings/tunnel")
            assert live.json()["state"] == "live"
            assert live.json()["url"] == TUNNEL_URL

            # The tunnel host is admitted only while live.
            via_tunnel = await client.get(
                "/api/v1/settings/tunnel",
                headers={"Host": TUNNEL_HOST},
            )
            assert via_tunnel.status_code == 200
            rejected = await client.get(
                "/api/v1/settings/tunnel", headers={"Host": "evil.example"}
            )
            assert rejected.status_code == 400

            stop = await client.post("/api/v1/settings/tunnel/stop", headers=headers)
            assert stop.status_code == 200
            assert stop.json()["state"] == "stopped"

            after = await client.get(
                "/api/v1/settings/tunnel", headers={"Host": TUNNEL_HOST}
            )
            assert after.status_code == 400

    asyncio.run(check())


def test_tunnel_origin_is_accepted_only_while_live(tmp_path: Path) -> None:
    _path, app, manager = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)

            over_tunnel = {
                "Origin": TUNNEL_URL,
                "X-CSRF-Token": headers["X-CSRF-Token"],
            }
            # Not live yet: the tunnel origin is rejected (Host stays loopback so
            # this exercises the origin gate, not the host gate).
            assert (
                await client.post("/api/v1/settings/tunnel/stop", headers=over_tunnel)
            ).status_code == 403

            await client.post("/api/v1/settings/tunnel/start", headers=headers)
            await _wait_for(lambda: manager.snapshot().state is TunnelState.LIVE)

            started = await client.post(
                "/api/v1/settings/tunnel/stop",
                headers={**over_tunnel, "Host": TUNNEL_HOST},
            )
            assert started.status_code == 200

    asyncio.run(check())


def test_login_over_tunnel_sets_secure_cookie(tmp_path: Path) -> None:
    _path, app, manager = _app(tmp_path)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ALLOWED_ORIGIN
        ) as client:
            headers = await _signed_in(client)
            await client.post("/api/v1/settings/tunnel/start", headers=headers)
            await _wait_for(lambda: manager.snapshot().state is TunnelState.LIVE)

            local = await client.post(
                "/api/v1/auth/login",
                json={"password": PASSWORD},
                headers={"Origin": ALLOWED_ORIGIN},
            )
            assert "secure" not in local.headers["set-cookie"].lower()

            remote = await client.post(
                "/api/v1/auth/login",
                json={"password": PASSWORD},
                headers={"Origin": TUNNEL_URL, "Host": TUNNEL_HOST},
            )
            assert remote.status_code == 200
            assert "secure" in remote.headers["set-cookie"].lower()

    asyncio.run(check())
