import asyncio
from pathlib import Path

import pytest

import bq.infra.tunnel as tunnel_module
from bq.infra.tunnel import (
    MAX_LOG_CHARS,
    MAX_LOG_LINE_CHARS,
    MAX_LOG_LINES,
    TunnelError,
    TunnelManager,
    TunnelState,
)

URL = "https://brave-unit-4821.trycloudflare.com"
NO_BINARY = "/nonexistent/cloudflared-not-installed"
READY = [
    "2026-11-26T14:24:12Z INF Your quick Tunnel has been created! Visit it at:",
    f"2026-11-26T14:24:12Z INF |  {URL}  |",
    "2026-11-26T14:24:13Z INF Registered tunnel connection connIndex=0 protocol=quic",
]


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


class Spawner:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.calls = 0
        self.argv: list[str] = []

    async def __call__(self, argv: object) -> FakeProcess:
        assert isinstance(argv, (list, tuple))
        self.argv = [str(item) for item in argv]
        self.calls += 1
        return FakeProcess(self.lines)


def _installer_for(directory: Path):  # type: ignore[no-untyped-def]
    async def install(bin_dir: Path) -> Path:
        bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        binary = bin_dir / "cloudflared"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o700)
        return binary

    del directory
    return install


async def _healthy(url: str) -> bool:
    return True


async def _sick(url: str) -> bool:
    return False


async def _wait_for(done: object) -> None:  # type: ignore[no-untyped-def]
    for _ in range(500):
        if done():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("tunnel did not reach the expected state")


@pytest.fixture(autouse=True)
def _no_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BQ_CLOUDFLARED", NO_BINARY)


def test_happy_path_publishes_a_quick_tunnel(tmp_path: Path) -> None:
    spawner = Spawner(READY)

    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=spawner,
            health_probe=_healthy,
            installer=_installer_for(tmp_path),
        )
        assert manager.allowed_hosts() == frozenset()
        await manager.request_start()
        await _wait_for(lambda: manager.snapshot().state is TunnelState.LIVE)
        status = manager.snapshot()
        assert status.url == URL
        assert status.error is None
        assert status.binary_path is not None
        assert manager.allowed_hosts() == frozenset(
            {"brave-unit-4821.trycloudflare.com"}
        )
        assert manager.allowed_origins() == frozenset({URL})
        assert spawner.calls == 1

        await manager.stop()
        assert manager.snapshot().state is TunnelState.STOPPED
        assert manager.allowed_hosts() == frozenset()
        assert manager.allowed_origins() == frozenset()

    asyncio.run(check())


def test_unhealthy_origin_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tunnel_module, "HEALTH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(tunnel_module, "HEALTH_POLL_SECONDS", 0.01)
    spawner = Spawner(READY)

    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=spawner,
            health_probe=_sick,
            installer=_installer_for(tmp_path),
        )
        await manager.request_start()
        await _wait_for(lambda: manager.snapshot().state is TunnelState.ERROR)
        status = manager.snapshot()
        assert status.url is None
        assert "healthy" in (status.error or "")
        assert manager.allowed_hosts() == frozenset()
        assert manager.allowed_origins() == frozenset()

    asyncio.run(check())


def test_missing_binary_without_install_fails(tmp_path: Path) -> None:
    spawner = Spawner([f"created {URL}"])

    async def no_binary(bin_dir: Path) -> Path:
        return bin_dir / "cloudflared"

    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=spawner,
            health_probe=_healthy,
            installer=no_binary,
        )
        await manager.request_start()
        await _wait_for(lambda: manager.snapshot().state is TunnelState.ERROR)
        assert manager.snapshot().error is not None
        assert manager.allowed_hosts() == frozenset()

    asyncio.run(check())


def test_duplicate_start_is_a_no_op(tmp_path: Path) -> None:
    spawner = Spawner(READY)

    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=spawner,
            health_probe=_healthy,
            installer=_installer_for(tmp_path),
        )
        await manager.request_start()
        await manager.request_start()
        await _wait_for(lambda: manager.snapshot().state is TunnelState.LIVE)
        assert spawner.calls == 1

    asyncio.run(check())


def test_duplicate_start_after_live_is_a_no_op(tmp_path: Path) -> None:
    spawner = Spawner(READY)

    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=spawner,
            health_probe=_healthy,
            installer=_installer_for(tmp_path),
        )
        await manager.request_start()
        await _wait_for(lambda: manager.snapshot().state is TunnelState.LIVE)
        await manager.request_start()
        assert spawner.calls == 1
        assert manager.snapshot().state is TunnelState.LIVE

    asyncio.run(check())


def test_non_trycloudflare_url_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tunnel_module, "START_TIMEOUT_SECONDS", 0.05)
    spawner = Spawner(
        ["listening on https://evil.example/path", "INF Registered tunnel connection"]
    )

    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=spawner,
            health_probe=_healthy,
            installer=_installer_for(tmp_path),
        )
        await manager.request_start()
        await _wait_for(lambda: manager.snapshot().state is TunnelState.ERROR)
        assert manager.snapshot().error is not None
        assert manager.allowed_hosts() == frozenset()

    asyncio.run(check())


def test_log_tail_is_bounded(tmp_path: Path) -> None:
    lines = [f"noise line {index} " + "x" * 200 for index in range(80)]
    spawner = Spawner([*lines, *READY])

    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=spawner,
            health_probe=_healthy,
            installer=_installer_for(tmp_path),
        )
        await manager.request_start()
        await _wait_for(lambda: manager.snapshot().state is TunnelState.LIVE)
        log = manager.snapshot().recent_log
        assert len(log) <= MAX_LOG_LINES
        assert sum(len(line) for line in log) <= MAX_LOG_CHARS

    asyncio.run(check())


def test_long_banner_line_keeps_more_than_the_error_bound(
    tmp_path: Path,
) -> None:
    banner = "INF " + "Z" * 1000
    spawner = Spawner([banner, *READY])

    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=spawner,
            health_probe=_healthy,
            installer=_installer_for(tmp_path),
        )
        await manager.request_start()
        await _wait_for(lambda: manager.snapshot().state is TunnelState.LIVE)
        stored = [line for line in manager.snapshot().recent_log if "Z" in line]
        assert stored
        assert len(stored[0]) == MAX_LOG_LINE_CHARS
        assert len(stored[0]) > 300

    asyncio.run(check())


def test_failure_preserves_the_last_edge_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tunnel_module, "START_TIMEOUT_SECONDS", 0.05)
    spawner = Spawner(
        [
            "INF Requesting new quick Tunnel on trycloudflare.com...",
            "ERR failed to request quick Tunnel: context deadline exceeded",
        ]
    )

    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=spawner,
            health_probe=_healthy,
            installer=_installer_for(tmp_path),
        )
        await manager.request_start()
        await _wait_for(lambda: manager.snapshot().state is TunnelState.ERROR)
        error = manager.snapshot().error or ""
        assert "context deadline exceeded" in error
        assert "ERR failed to request quick Tunnel" in error

    asyncio.run(check())


def test_start_timeout_reports_a_real_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tunnel_module, "START_TIMEOUT_SECONDS", 0.05)

    class StallingReader:
        async def readline(self) -> bytes:
            await asyncio.sleep(3600)
            return b""

    class StallingProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stderr = StallingReader()
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

    async def spawn(argv: object) -> StallingProcess:
        del argv
        return StallingProcess()

    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=spawn,
            health_probe=_healthy,
            installer=_installer_for(tmp_path),
        )
        await manager.request_start()
        await _wait_for(lambda: manager.snapshot().state is TunnelState.ERROR)
        assert "usable tunnel" in (manager.snapshot().error or "")
        assert manager.allowed_hosts() == frozenset()

    asyncio.run(check())


def test_stop_is_idempotent_before_start(tmp_path: Path) -> None:
    async def check() -> None:
        manager = TunnelManager(
            tmp_path,
            spawn=Spawner([]),
            health_probe=_healthy,
            installer=_installer_for(tmp_path),
        )
        status = await manager.stop()
        assert status.state is TunnelState.STOPPED
        assert manager.allowed_hosts() == frozenset()

    asyncio.run(check())


def test_release_asset_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tunnel_module.sys, "platform", "darwin")
    monkeypatch.setattr(tunnel_module.platform, "machine", lambda: "arm64")
    assert tunnel_module._release_asset() == "cloudflared-darwin-arm64.tgz"

    monkeypatch.setattr(tunnel_module.sys, "platform", "linux")
    monkeypatch.setattr(tunnel_module.platform, "machine", lambda: "x86_64")
    assert tunnel_module._release_asset() == "cloudflared-linux-amd64"

    monkeypatch.setattr(tunnel_module.sys, "platform", "plan9")
    with pytest.raises(TunnelError):
        tunnel_module._release_asset()
