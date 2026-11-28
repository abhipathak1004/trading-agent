"""File-backed operational logbook for LLM calls and decisions.

Design
------
* One JSON object per line. ``record`` appends to a spool file
  (``.pending.jsonl``); ``flush`` moves the spool into dated files and prunes
  files older than the configured retention. The server flushes every 15
  minutes by default, and a record can also trigger the flush once its interval
  has elapsed.
* Cross-process safe: appending and flushing take an ``fcntl`` lock on a
  sibling file. A flush renames the spool to a unique processing file before
  writing, so records that arrive during a flush land in a fresh spool and are
  neither lost nor duplicated.
* A logging failure must never break a trade decision. ``record`` swallows I/O
  and serialization errors and counts them in ``dropped``.
* Full prompts and responses are stored because the owner asked for them. Every
  text field is bounded, the whole entry is bounded, and bearer/API-key-looking
  substrings are redacted before anything reaches disk.

This is operational telemetry, deliberately separate from the hash-chained
``audit_events`` table (which records state-changing owner actions).
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from bq.core import Clock, SystemClock, parse_timestamp, timestamp, utc

LOG_SCHEMA_VERSION = 1
LOG_DIR_NAME = "logs"
PENDING_NAME = ".pending.jsonl"
LOCK_NAME = ".flush.lock"
PROCESSING_PREFIX = ".processing-"
FILE_PREFIX = "bq-"
FILE_SUFFIX = ".jsonl"

MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650
DEFAULT_RETENTION_DAYS = 14
MIN_FLUSH_MINUTES = 1
MAX_FLUSH_MINUTES = 1440
DEFAULT_FLUSH_MINUTES = 15

MAX_TEXT_BYTES = 256 * 1024
MAX_ENTRY_BYTES = 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_MAPPING_KEYS = 64
MAX_SEQUENCE_ITEMS = 256
MAX_SANITIZE_DEPTH = 8
MAX_RECENT_ENTRIES = 200
MAX_RECENT_BYTES = 1024 * 1024
MAX_LISTED_FILES = 200
TRUNCATION_MARKER = "…[truncated]"
STALE_PROCESSING_SECONDS = 24 * 60 * 60

LOG_KINDS = frozenset({"llm_call", "decision", "system"})
LOG_LEVELS = frozenset({"info", "warning", "error"})

_FILE_NAME = re.compile(r"^bq-(\d{4}-\d{2}-\d{2})(?:\.(\d+))?\.jsonl$")
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "sk-[redacted]"),
    (
        re.compile(r"(?i)(x-goog-api-key\s*[:=]\s*)[^\s\"',}]+"),
        r"\1[redacted]",
    ),
    (
        re.compile(r"(?i)(\"?(?:api[_-]?key|access[_-]?token)\"?\s*[:=]\s*\")[^\"]+"),
        r"\1[redacted]",
    ),
)


class LoggingConfigError(ValueError):
    """A logging configuration value is out of bounds."""


def validate_enabled(value: object) -> bool:
    if not isinstance(value, bool):
        raise LoggingConfigError("Logging enabled must be a boolean.")
    return value


def validate_retention_days(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoggingConfigError("Log retention must be a whole number of days.")
    if not MIN_RETENTION_DAYS <= value <= MAX_RETENTION_DAYS:
        raise LoggingConfigError(
            f"Log retention must be between {MIN_RETENTION_DAYS} and "
            f"{MAX_RETENTION_DAYS} days."
        )
    return value


def validate_flush_minutes(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoggingConfigError(
            "Log flush interval must be a whole number of minutes."
        )
    if not MIN_FLUSH_MINUTES <= value <= MAX_FLUSH_MINUTES:
        raise LoggingConfigError(
            f"Log flush interval must be between {MIN_FLUSH_MINUTES} and "
            f"{MAX_FLUSH_MINUTES} minutes."
        )
    return value


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    enabled: bool = True
    retention_days: int = DEFAULT_RETENTION_DAYS
    flush_minutes: int = DEFAULT_FLUSH_MINUTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", validate_enabled(self.enabled))
        object.__setattr__(
            self, "retention_days", validate_retention_days(self.retention_days)
        )
        object.__setattr__(
            self, "flush_minutes", validate_flush_minutes(self.flush_minutes)
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> LoggingConfig:
        return cls(
            enabled=validate_enabled(value.get("enabled", True)),
            retention_days=validate_retention_days(
                value.get("retention_days", DEFAULT_RETENTION_DAYS)
            ),
            flush_minutes=validate_flush_minutes(
                value.get("flush_minutes", DEFAULT_FLUSH_MINUTES)
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "retention_days": self.retention_days,
            "flush_minutes": self.flush_minutes,
        }


@dataclass(frozen=True, slots=True)
class LogFile:
    name: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class FlushResult:
    written: int = 0
    files: tuple[str, ...] = ()
    pruned: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "written": self.written,
            "files": list(self.files),
            "pruned": list(self.pruned),
        }


def _sanitize_text(value: str) -> tuple[str, bool]:
    text = value
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES:
        return text, False
    cut = encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
    return f"{cut}{TRUNCATION_MARKER}", True


def _sanitize(value: object, *, depth: int = 0) -> tuple[object, bool]:
    if depth > MAX_SANITIZE_DEPTH:
        return None, True
    if value is None or isinstance(value, bool):
        return value, False
    if isinstance(value, int):
        return value, False
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (None, True)
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, datetime):
        return timestamp(value), False
    if isinstance(value, StrEnum):
        return value.value, False
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        truncated = False
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_MAPPING_KEYS:
                truncated = True
                break
            clean, item_truncated = _sanitize(item, depth=depth + 1)
            result[str(key)[:128]] = clean
            truncated = truncated or item_truncated
        return result, truncated
    if isinstance(value, list | tuple):
        items = list(value)
        truncated = len(items) > MAX_SEQUENCE_ITEMS
        result_items: list[object] = []
        for item in items[:MAX_SEQUENCE_ITEMS]:
            clean, item_truncated = _sanitize(item, depth=depth + 1)
            result_items.append(clean)
            truncated = truncated or item_truncated
        return result_items, truncated
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _sanitize(to_dict(), depth=depth + 1)
        except Exception:  # noqa: BLE001 - logging must never raise
            return f"<{type(value).__name__}>", True
    return f"<{type(value).__name__}>", False


class Logbook:
    """Append-only spool plus dated files under ``<home>/logs``."""

    def __init__(
        self,
        home: Path,
        config: LoggingConfig,
        *,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(config, LoggingConfig):
            raise LoggingConfigError("A logging configuration is required.")
        self.home = Path(home)
        self.directory = self.home / LOG_DIR_NAME
        self.pending_path = self.directory / PENDING_NAME
        self.clock = clock or SystemClock()
        self._config = config
        self._thread_lock = threading.Lock()
        self._last_flush = time.monotonic()
        self.dropped = 0

    @property
    def config(self) -> LoggingConfig:
        return self._config

    def configure(self, config: LoggingConfig) -> None:
        if not isinstance(config, LoggingConfig):
            raise LoggingConfigError("A logging configuration is required.")
        self._config = config

    # -- recording ---------------------------------------------------------

    def record(
        self,
        kind: str,
        *,
        level: str = "info",
        data: Mapping[str, object] | None = None,
        **fields: object,
    ) -> None:
        if not self._config.enabled or kind not in LOG_KINDS:
            return
        merged: dict[str, object] = dict(data or {})
        merged.update(fields)
        try:
            now = utc(self.clock.now())
            clean, truncated = _sanitize(merged)
            payload: dict[str, object] = {
                "v": LOG_SCHEMA_VERSION,
                "ts": timestamp(now),
                "kind": kind,
                "level": level if level in LOG_LEVELS else "info",
                "truncated": truncated,
                "data": clean,
            }
            encoded = self._encode(payload)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.dropped += 1
            return
        if not self._append(encoded):
            return
        self.maybe_flush()

    def _encode(self, payload: dict[str, object]) -> bytes:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) <= MAX_ENTRY_BYTES:
            return encoded
        fallback = dict(payload)
        fallback["truncated"] = True
        fallback["data"] = {"error": "entry_over_limit"}
        return json.dumps(
            fallback, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    def _append(self, encoded: bytes) -> bool:
        try:
            with self._locked():
                descriptor = os.open(
                    self.pending_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                try:
                    view = memoryview(encoded + b"\n")
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("Log append made no progress.")
                        view = view[written:]
                finally:
                    os.close(descriptor)
        except OSError:
            self.dropped += 1
            return False
        return True

    def maybe_flush(self) -> None:
        if not self._config.enabled:
            return
        interval = self._config.flush_minutes * 60.0
        if time.monotonic() - self._last_flush < interval:
            return
        try:
            self.flush()
        except Exception:  # noqa: BLE001 - logging must never raise
            self.dropped += 1

    # -- flushing ----------------------------------------------------------

    def flush(self) -> FlushResult:
        with self._locked():
            written = 0
            files: list[str] = []
            if self.pending_path.exists():
                processing = self.directory / (
                    f"{PROCESSING_PREFIX}{secrets.token_hex(8)}{FILE_SUFFIX}"
                )
                try:
                    os.replace(self.pending_path, processing)
                except OSError:
                    self._last_flush = time.monotonic()
                    return FlushResult()
                try:
                    written, files = self._write_processing(processing)
                except OSError:
                    self._requeue(processing)
                    self.dropped += 1
                    written, files = 0, []
                finally:
                    try:
                        processing.unlink(missing_ok=True)
                    except OSError:
                        pass
            pruned = self._prune_locked()
            self._last_flush = time.monotonic()
        return FlushResult(written=written, files=tuple(files), pruned=tuple(pruned))

    def prune(self) -> tuple[str, ...]:
        with self._locked():
            return tuple(self._prune_locked())

    def _write_processing(self, path: Path) -> tuple[int, list[str]]:
        groups: dict[str, list[bytes]] = {}
        dropped = 0
        with open(path, "rb") as handle:
            for raw in handle:
                line = raw.rstrip(b"\n")
                if not line:
                    continue
                day = self._entry_day(line)
                if day is None:
                    dropped += 1
                    continue
                groups.setdefault(day, []).append(line)
        self.dropped += dropped
        written = 0
        files: list[str] = []
        for day in sorted(groups):
            lines = groups[day]
            written += len(lines)
            files.extend(self._write_day(day, lines))
        return written, files

    @staticmethod
    def _entry_day(line: bytes) -> str | None:
        try:
            payload = json.loads(line)
            instant = parse_timestamp(str(payload["ts"]))
            return utc(instant).date().isoformat()
        except (ValueError, KeyError, TypeError):
            return None

    def _write_day(self, day: str, lines: list[bytes]) -> list[str]:
        files: list[str] = []
        batch: list[bytes] = []
        size = 0
        for line in lines:
            extra = len(line) + 1
            if batch and size + extra > MAX_FILE_BYTES:
                files.append(self._append_batch(day, batch))
                batch = []
                size = 0
            batch.append(line)
            size += extra
        if batch:
            files.append(self._append_batch(day, batch))
        return files

    def _append_batch(self, day: str, batch: list[bytes]) -> str:
        payload = b"".join(line + b"\n" for line in batch)
        index = 0
        while True:
            target = self._segment(day, index)
            current = target.stat().st_size if target.exists() else 0
            if current == 0 or current + len(payload) <= MAX_FILE_BYTES:
                with open(target, "ab") as handle:
                    handle.write(payload)
                return target.name
            index += 1

    def _segment(self, day: str, index: int) -> Path:
        name = f"{FILE_PREFIX}{day}{FILE_SUFFIX}"
        if index:
            name = f"{FILE_PREFIX}{day}.{index}{FILE_SUFFIX}"
        return self.directory / name

    def _requeue(self, processing: Path) -> None:
        try:
            data = processing.read_bytes()
            if not data:
                return
            with open(self.pending_path, "ab") as handle:
                handle.write(data)
        except OSError:
            pass

    def _prune_locked(self) -> list[str]:
        cutoff = utc(self.clock.now()).date() - timedelta(
            days=self._config.retention_days
        )
        pruned: list[str] = []
        for path in self._data_files():
            match = _FILE_NAME.match(path.name)
            if match is None:
                continue
            try:
                file_day = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if file_day >= cutoff:
                continue
            try:
                path.unlink()
                pruned.append(path.name)
            except OSError:
                pass
        now = time.time()
        for stale in self.directory.glob(f"{PROCESSING_PREFIX}*"):
            try:
                if now - stale.stat().st_mtime <= STALE_PROCESSING_SECONDS:
                    continue
                stale.unlink()
                pruned.append(stale.name)
            except OSError:
                pass
        return pruned

    # -- reads -------------------------------------------------------------

    def _data_files(self) -> list[Path]:
        try:
            return sorted(
                path
                for path in self.directory.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}")
                if _FILE_NAME.match(path.name) is not None
            )
        except OSError:
            return []

    def pending_bytes(self) -> int:
        try:
            return self.pending_path.stat().st_size
        except OSError:
            return 0

    def files(self) -> tuple[LogFile, ...]:
        listed: list[LogFile] = []
        for path in self._data_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            listed.append(
                LogFile(
                    name=path.name,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                )
            )
        listed.sort(key=lambda item: (item.modified_at, item.name), reverse=True)
        return tuple(listed[:MAX_LISTED_FILES])

    def recent(self, limit: int = 50) -> tuple[dict[str, object], ...]:
        bounded = max(1, min(limit, MAX_RECENT_ENTRIES))
        lines: list[bytes] = []
        if self.pending_path.exists():
            lines.extend(self._tail_lines(self.pending_path, bounded))
        for path in reversed(self._data_files()):
            if len(lines) >= bounded:
                break
            lines.extend(self._tail_lines(path, bounded - len(lines)))
        entries: list[dict[str, object]] = []
        for raw in lines:
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if isinstance(payload, dict):
                entries.append(payload)
        entries.sort(key=lambda item: str(item.get("ts", "")), reverse=True)
        return tuple(entries[:bounded])

    @staticmethod
    def _tail_lines(path: Path, limit: int) -> list[bytes]:
        if limit <= 0:
            return []
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                position = size
                chunk = b""
                while (
                    position > 0
                    and chunk.count(b"\n") <= limit
                    and size - position < MAX_RECENT_BYTES
                ):
                    step = min(65536, position)
                    position -= step
                    handle.seek(position)
                    chunk = handle.read(step) + chunk
        except OSError:
            return []
        lines = chunk.splitlines()
        if position > 0 and lines:
            lines = lines[1:]
        return lines[-limit:]

    def status(self) -> dict[str, object]:
        return {
            "enabled": self._config.enabled,
            "retention_days": self._config.retention_days,
            "flush_minutes": self._config.flush_minutes,
            "directory": str(self.directory),
            "pending_bytes": self.pending_bytes(),
            "dropped": self.dropped,
            "files": [
                {
                    "name": item.name,
                    "size_bytes": item.size_bytes,
                    "modified_at": timestamp(item.modified_at),
                }
                for item in self.files()
            ],
            "recent": list(self.recent(limit=50)),
        }

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._thread_lock:
            with open(self.directory / LOCK_NAME, "a", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class NullLogbook:
    """No-op logbook used before configuration and in unit tests."""

    dropped = 0

    @property
    def config(self) -> LoggingConfig:
        return LoggingConfig()

    def configure(self, config: LoggingConfig) -> None:
        del config

    def record(
        self,
        kind: str,
        *,
        level: str = "info",
        data: Mapping[str, object] | None = None,
        **fields: object,
    ) -> None:
        del kind, level, data, fields

    def maybe_flush(self) -> None:
        return

    def flush(self) -> FlushResult:
        return FlushResult()

    def prune(self) -> tuple[str, ...]:
        return ()

    def files(self) -> tuple[LogFile, ...]:
        return ()

    def recent(self, limit: int = 50) -> tuple[dict[str, object], ...]:
        del limit
        return ()

    def status(self) -> dict[str, object]:
        config = self.config
        return {
            "enabled": config.enabled,
            "retention_days": config.retention_days,
            "flush_minutes": config.flush_minutes,
            "directory": "",
            "pending_bytes": 0,
            "dropped": 0,
            "files": [],
            "recent": [],
        }


_NULL_LOGBOOK = NullLogbook()
_LOGBOOK: NullLogbook | Logbook = _NULL_LOGBOOK
_LOGBOOK_LOCK = threading.Lock()


def configure_logbook(home: Path, config: LoggingConfig) -> Logbook:
    """Install the process-wide logbook for this home directory."""
    book = Logbook(Path(home), config)
    global _LOGBOOK
    with _LOGBOOK_LOCK:
        _LOGBOOK = book
    return book


def get_logbook() -> NullLogbook | Logbook:
    return _LOGBOOK


def reset_logbook() -> None:
    global _LOGBOOK
    with _LOGBOOK_LOCK:
        _LOGBOOK = _NULL_LOGBOOK


def record_llm_call(
    *,
    provider: str,
    model: str,
    task_id: str,
    latency_ms: float,
    ok: bool,
    task_version: str = "v1",
    agent_id: str | None = None,
    tier: str | None = None,
    battery: str | None = None,
    error: str | None = None,
    finish_reason: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    system: str = "",
    prompt: str = "",
    response: str = "",
    reasoning: str = "",
) -> None:
    """Record one provider call. API keys are never passed to this function."""
    data: dict[str, object] = {
        "provider": provider,
        "model": model,
        "task_id": task_id,
        "task_version": task_version,
        "latency_ms": round(float(latency_ms), 3),
        "ok": bool(ok),
        "finish_reason": finish_reason,
        "system": system,
        "prompt": prompt,
        "response": response,
    }
    if agent_id is not None:
        data["agent_id"] = agent_id
    if tier is not None:
        data["tier"] = tier
    if battery is not None:
        data["battery"] = battery
    if error is not None:
        data["error"] = error
    if input_tokens is not None:
        data["input_tokens"] = input_tokens
    if output_tokens is not None:
        data["output_tokens"] = output_tokens
    if reasoning:
        data["reasoning"] = reasoning
    get_logbook().record("llm_call", data=data)


def record_decision(scope: str, **fields: Any) -> None:
    """Record one trading/analysis decision under a named scope."""
    data: dict[str, object] = {"scope": scope}
    data.update(fields)
    get_logbook().record("decision", data=data)


__all__ = [
    "DEFAULT_FLUSH_MINUTES",
    "DEFAULT_RETENTION_DAYS",
    "MAX_FLUSH_MINUTES",
    "MAX_RETENTION_DAYS",
    "MIN_FLUSH_MINUTES",
    "MIN_RETENTION_DAYS",
    "FlushResult",
    "LogFile",
    "LoggingConfig",
    "LoggingConfigError",
    "Logbook",
    "NullLogbook",
    "configure_logbook",
    "get_logbook",
    "record_decision",
    "record_llm_call",
    "reset_logbook",
    "validate_enabled",
    "validate_flush_minutes",
    "validate_retention_days",
]
