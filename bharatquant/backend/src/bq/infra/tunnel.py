"""Owner-triggered Cloudflare quick tunnel.

A quick tunnel (``cloudflared tunnel --url ...``) is ephemeral: it returns a
``*.trycloudflare.com`` URL that lives only while the child process runs. Nothing
about it is persisted, so there is no setting row and no migration.

Two deliberate limits:

* The manager is the only writer of tunnel state. It is injected into the API so
  the host/origin allowlists are non-empty only while the tunnel is actually
  live; a stopped or failed tunnel fails closed to loopback.
* The binary is never fetched from an owner-supplied URL. An install runs only
  on an explicit Start, only from the official GitHub release, and is verified
  by running ``--version`` before the path is used.
"""

import asyncio
import os
import platform
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from bq.core import (
    Clock,
    SystemClock,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    utc as core_utc,
)
from bq.infra.audit import append_event

LOCAL_ORIGIN = "http://127.0.0.1:8000"
HEALTH_PATH = "/api/v1/system/health"
RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"
MAX_ERROR_CHARS = 500
MAX_LOG_CHARS = 4096
MAX_LOG_LINE_CHARS = 600
MAX_LOG_LINES = 40
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
START_TIMEOUT_SECONDS = 60.0
HEALTH_TIMEOUT_SECONDS = 20.0
HEALTH_POLL_SECONDS = 1.0
TERMINATE_TIMEOUT_SECONDS = 5.0
_VERSION_TIMEOUT_SECONDS = 10.0

_TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_REGISTERED = re.compile(r"Registered tunnel connection")
_BINARY_NAME = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"


class TunnelError(RuntimeError):
    pass


class TunnelState(StrEnum):
    STOPPED = "stopped"
    DOWNLOADING = "downloading"
    STARTING = "starting"
    LIVE = "live"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TunnelStatus:
    state: TunnelState = TunnelState.STOPPED
    url: str | None = None
    started_at: datetime | None = None
    error: str | None = None
    binary_path: str | None = None
    recent_log: tuple[str, ...] = ()


class LineReader(Protocol):
    async def readline(self) -> bytes: ...


class TunnelProcess(Protocol):
    @property
    def returncode(self) -> int | None: ...

    @property
    def stderr(self) -> LineReader | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


Spawn = Callable[[Sequence[str]], Awaitable[TunnelProcess]]
HealthProbe = Callable[[str], Awaitable[bool]]
Installer = Callable[[Path], Awaitable[Path]]


async def _default_spawn(argv: Sequence[str]) -> TunnelProcess:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


async def _default_health_probe(url: str) -> bool:
    """Check the local origin answers its health path.

    This deliberately targets the loopback origin, not the public tunnel URL.
    A freshly created ``*.trycloudflare.com`` name is routinely negatively
    cached by ISP resolvers for a minute or more, so probing the public name
    fails on DNS long after the tunnel is actually up. Reachability of the
    public link is proven by cloudflared's own "Registered tunnel connection"
    line; the origin check here proves the app behind it is serving.
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0), follow_redirects=True
        ) as client:
            response = await client.get(url.rstrip("/") + HEALTH_PATH)
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def _release_asset() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "cloudflared-darwin-arm64.tgz"
        if machine in {"x86_64", "amd64"}:
            return "cloudflared-darwin-amd64.tgz"
    elif sys.platform.startswith("linux"):
        if machine in {"arm64", "aarch64"}:
            return "cloudflared-linux-arm64"
        if machine in {"x86_64", "amd64"}:
            return "cloudflared-linux-amd64"
    elif sys.platform == "win32" and machine in {"x86_64", "amd64"}:
        return "cloudflared-windows-amd64.exe"
    raise TunnelError(
        "cloudflared is not available for this platform; set BQ_CLOUDFLARED."
    )


class TunnelManager:
    def __init__(
        self,
        home: Path,
        *,
        clock: Clock | None = None,
        database_path: Path | None = None,
        local_origin: str = LOCAL_ORIGIN,
        spawn: Spawn | None = None,
        health_probe: HealthProbe | None = None,
        installer: Installer | None = None,
    ) -> None:
        self.home = home
        self.clock = clock or SystemClock()
        self.database_path = database_path
        self.local_origin = local_origin
        self._spawn = spawn or _default_spawn
        self._health_probe = health_probe or _default_health_probe
        self._installer = installer or install_cloudflared
        self._status = TunnelStatus()
        self._log: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._binary: Path | None = self._resolve_binary()
        self._process: TunnelProcess | None = None
        self._runner: asyncio.Task[None] | None = None
        self._drainer: asyncio.Task[None] | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def _bin_dir(self) -> Path:
        return self.home / "bin"

    def _local_binary(self) -> Path:
        return self._bin_dir() / _BINARY_NAME

    def _resolve_binary(self) -> Path | None:
        override = os.environ.get("BQ_CLOUDFLARED")
        if override:
            candidate = Path(override).expanduser()
            if candidate.is_file():
                return candidate
        found = shutil.which("cloudflared")
        if found:
            return Path(found)
        local = self._local_binary()
        return local if local.is_file() else None

    def snapshot(self) -> TunnelStatus:
        self._binary = self._resolve_binary()
        return replace(
            self._status,
            recent_log=tuple(self._log),
            binary_path=None if self._binary is None else str(self._binary),
        )

    def allowed_hosts(self) -> frozenset[str]:
        if (
            self._status.state not in (TunnelState.STARTING, TunnelState.LIVE)
            or self._status.url is None
        ):
            return frozenset()
        host = urlsplit(self._status.url).hostname
        return frozenset() if host is None else frozenset({host})

    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"https://{host}" for host in self.allowed_hosts())

    async def request_start(self) -> TunnelStatus:
        async with self._lock:
            if self._status.state in (
                TunnelState.DOWNLOADING,
                TunnelState.STARTING,
                TunnelState.LIVE,
                TunnelState.STOPPING,
            ):
                return self.snapshot()
            self._binary = self._resolve_binary()
            state = (
                TunnelState.STARTING
                if self._binary is not None
                else TunnelState.DOWNLOADING
            )
            self._status = TunnelStatus(state=state, binary_path=None)
            self._log.clear()
            self._runner = asyncio.create_task(self._run())
        return self.snapshot()

    async def stop(self) -> TunnelStatus:
        async with self._lock:
            if self._status.state is TunnelState.STOPPED and self._process is None:
                return self.snapshot()
            was_running = self._status.state in (
                TunnelState.DOWNLOADING,
                TunnelState.STARTING,
                TunnelState.LIVE,
            )
            self._status = replace(self._status, state=TunnelState.STOPPING)
            runner = self._runner
            self._runner = None
        await _cancel(runner)
        self._status = replace(self._status, state=TunnelState.STOPPING, url=None)
        await self._terminate_process()
        self._status = TunnelStatus(state=TunnelState.STOPPED)
        if was_running:
            self._audit("tunnel.stopped", {"state": TunnelState.STOPPED.value})
        return self.snapshot()

    async def _run(self) -> None:
        try:
            binary = self._binary
            if binary is None:
                self._set_state(TunnelState.DOWNLOADING)
                binary = await self._installer(self._bin_dir())
                if not binary.is_file():
                    raise TunnelError("cloudflared installation produced no binary.")
                self._binary = binary
            self._set_state(TunnelState.STARTING)
            await self._launch(binary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(str(exc))

    async def _launch(self, binary: Path) -> None:
        self._append_log(f"starting {binary}")
        process = await self._spawn(
            [str(binary), "tunnel", "--url", self.local_origin, "--no-autoupdate"]
        )
        self._process = process
        reader = process.stderr
        if reader is None:
            raise TunnelError("cloudflared produced no output stream.")
        try:
            url = await asyncio.wait_for(
                self._read_ready(reader), timeout=START_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise TunnelError(
                "Cloudflare did not return a usable tunnel in time."
            ) from exc
        # Publish the URL while still STARTING so the origin check and any
        # request racing the start both see the host/origin gates opened.
        self._status = TunnelStatus(state=TunnelState.STARTING, url=url)
        self._drainer = asyncio.create_task(self._drain(reader))
        await self._await_health()
        started_at = core_utc(self.clock.now())
        self._status = TunnelStatus(
            state=TunnelState.LIVE, url=url, started_at=started_at
        )
        self._append_log(f"live at {url}")
        self._audit("tunnel.started", {"state": TunnelState.LIVE.value, "url": url})
        self._watcher = asyncio.create_task(self._watch(process))

    async def _read_ready(self, reader: LineReader) -> str:
        """Wait for both the public URL and the connector registration.

        Registration is the deterministic liveness signal: it is emitted when
        cloudflared's edge connection is established, and unlike a request to
        the public hostname it does not depend on DNS for a name that was
        created seconds ago.
        """
        url: str | None = None
        registered = False
        while url is None or not registered:
            raw = await reader.readline()
            if not raw:
                raise TunnelError("cloudflared exited before the tunnel was ready.")
            line = raw.decode("utf-8", errors="replace").strip()
            self._append_log(line)
            if url is None:
                match = _TUNNEL_URL.search(line)
                if match is not None:
                    url = match.group(0)
            if _REGISTERED.search(line) is not None:
                registered = True
        return url

    async def _drain(self, reader: LineReader) -> None:
        while True:
            raw = await reader.readline()
            if not raw:
                return
            self._append_log(raw.decode("utf-8", errors="replace").strip())

    async def _await_health(self) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        while True:
            process = self._process
            if process is not None and process.returncode is not None:
                raise TunnelError(f"cloudflared exited with code {process.returncode}.")
            if await self._health_probe(self.local_origin):
                return
            if time.monotonic() >= deadline:
                raise TunnelError("The local origin did not become healthy.")
            await asyncio.sleep(HEALTH_POLL_SECONDS)

    async def _watch(self, process: TunnelProcess) -> None:
        code = await process.wait()
        if self._status.state is TunnelState.LIVE:
            self._status = replace(self._status, state=TunnelState.STOPPING)
            await self._fail(f"cloudflared exited with code {code}.")

    async def _fail(self, message: str) -> None:
        detail = self._last_edge_error()
        await self._terminate_process()
        if detail is not None and detail not in message:
            message = f"{message} {detail}"
        self._status = TunnelStatus(
            state=TunnelState.ERROR,
            error=_bounded(message),
        )
        self._append_log(_bounded(message))
        self._audit("tunnel.start_failed", {"state": TunnelState.ERROR.value})

    def _last_edge_error(self) -> str | None:
        for line in reversed(self._log):
            lowered = line.lower()
            if " err " in lowered or lowered.startswith("err") or "failed" in lowered:
                return line
        return None

    async def _terminate_process(self) -> None:
        await _cancel(self._watcher)
        self._watcher = None
        await _cancel(self._drainer)
        self._drainer = None
        process = self._process
        self._process = None
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_TIMEOUT_SECONDS)
        except TimeoutError:
            process.kill()
            with suppress(Exception):
                await process.wait()

    def _set_state(self, state: TunnelState) -> None:
        self._status = TunnelStatus(state=state)

    def _append_log(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        self._log.append(text[:MAX_LOG_LINE_CHARS])
        while self._log and sum(len(item) for item in self._log) > MAX_LOG_CHARS:
            self._log.popleft()

    def _audit(self, action: str, payload: dict[str, str]) -> None:
        if self.database_path is None:
            return
        try:
            with closing(
                core_open_connection(self.database_path, foreign_keys=False)
            ) as connection:
                connection.execute("BEGIN IMMEDIATE")
                with connection:
                    append_event(
                        connection,
                        occurred_at=core_utc(self.clock.now()),
                        actor="owner",
                        action=action,
                        object_type="tunnel",
                        object_id="cloudflare_quick_tunnel",
                        payload=payload,
                    )
        except (OSError, sqlite3.Error, ValueError):
            # Audit is best-effort: it must never block the tunnel response.
            pass


def _bounded(message: str) -> str:
    text = " ".join(message.split())
    return text[:MAX_ERROR_CHARS] or "The tunnel failed."


async def _cancel(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def _download(url: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    with httpx.stream(
        "GET", url, timeout=httpx.Timeout(120.0), follow_redirects=True
    ) as response:
        if response.status_code != 200:
            raise TunnelError("The cloudflared release download failed.")
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise TunnelError("The cloudflared download exceeded its size bound.")
            chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        raise TunnelError("The cloudflared download was empty.")
    return payload


def _extract(payload: bytes, asset: str, target: Path) -> None:
    with tempfile.TemporaryDirectory() as directory:
        if asset.endswith(".tgz"):
            archive = Path(directory) / asset
            archive.write_bytes(payload)
            with tarfile.open(archive, "r:gz") as tar:
                member = next(
                    (
                        item
                        for item in tar.getmembers()
                        if item.isfile() and item.name.endswith(_BINARY_NAME)
                    ),
                    None,
                )
                if member is None:
                    raise TunnelError("The cloudflared archive had no binary.")
                source = tar.extractfile(member)
                if source is None:
                    raise TunnelError("The cloudflared archive was unreadable.")
                target.write_bytes(source.read())
        else:
            target.write_bytes(payload)
    target.chmod(0o700)


async def install_cloudflared(bin_dir: Path) -> Path:
    """Download the official cloudflared build into ``bin_dir``.

    Only GitHub's own release URL is used, the response is size-bounded, and the
    resulting binary must answer ``--version`` before it is returned.
    """
    asset = _release_asset()
    target = bin_dir / _BINARY_NAME
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = _download(f"{RELEASE_BASE}/{asset}")
    _extract(payload, asset, target)
    await _verify_binary(target)
    return target


async def _verify_binary(binary: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=_VERSION_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        process.kill()
        raise TunnelError("The downloaded cloudflared did not respond.") from exc
    if process.returncode != 0 or not stdout.decode(
        "utf-8", errors="replace"
    ).startswith("cloudflared version"):
        raise TunnelError("The downloaded cloudflared binary is not valid.")
