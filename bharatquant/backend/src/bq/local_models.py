"""Registry of local open-source models available to evaluations and routing.

The registry is configuration, not run state: it is persisted in
``app_settings.local_models`` and read back as :class:`LocalModel` values. A
model's ``id`` is the Promptfoo provider id (``ollama:chat:<model>`` or
``openai:chat:<model>``), so the same key identifies a model in the eval
launcher, the eval runner, and the settings picker.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from bq.core import parse_timestamp as core_parse_timestamp

ModelKind = Literal["ollama", "openai_compatible"]
MODEL_KINDS: tuple[ModelKind, ...] = ("ollama", "openai_compatible")

DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434/v1"
MAX_LOCAL_MODELS = 32
MAX_LABEL_CHARS = 80
MAX_MODEL_NAME_CHARS = 200
MAX_ENDPOINT_CHARS = 4096
MIN_CONTEXT_TOKENS = 256
MAX_CONTEXT_TOKENS = 131_072
MIN_PREDICT_TOKENS = 32
MAX_PREDICT_TOKENS = 16_384
MAX_TEMPERATURE = 2.0

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LocalModelError(ValueError):
    """A model registry entry failed validation."""


@dataclass(frozen=True, slots=True)
class LocalModel:
    id: str
    label: str
    kind: ModelKind
    model: str
    endpoint: str | None
    num_ctx: int = 16_384
    num_predict: int = 512
    temperature: float = 0.0
    think: bool = False
    created_at: datetime | None = None

    @property
    def resolved_endpoint(self) -> str:
        if self.endpoint:
            return self.endpoint
        if self.kind == "ollama":
            return DEFAULT_OLLAMA_ENDPOINT
        return ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "model": self.model,
            "endpoint": self.endpoint,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
            "temperature": self.temperature,
            "think": self.think,
            "created_at": (
                None if self.created_at is None else self.created_at.isoformat()
            ),
        }


def provider_id(kind: ModelKind, model: str) -> str:
    prefix = "ollama:chat:" if kind == "ollama" else "openai:chat:"
    return prefix + model


def _text(value: object, label: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise LocalModelError(f"{label} must be text.")
    text = value.strip()
    if not text and not allow_empty:
        raise LocalModelError(f"{label} is required.")
    if len(text) > limit:
        raise LocalModelError(f"{label} must be at most {limit} characters.")
    if _CONTROL.search(text):
        raise LocalModelError(f"{label} must not contain control characters.")
    return text


def _kind(value: object) -> ModelKind:
    if value not in MODEL_KINDS:
        raise LocalModelError("Backend must be ollama or openai_compatible.")
    return value


def _endpoint(value: object, kind: ModelKind) -> str | None:
    if value is None or value == "":
        if kind == "openai_compatible":
            raise LocalModelError("An OpenAI-compatible model needs an endpoint.")
        return None
    endpoint = _text(value, "Endpoint", MAX_ENDPOINT_CHARS)
    parts = urlsplit(endpoint)
    if parts.username or parts.password:
        raise LocalModelError("Endpoint must not embed credentials.")
    if parts.scheme == "https" and parts.hostname:
        return endpoint
    if parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS:
        return endpoint
    raise LocalModelError("Endpoint must use HTTPS, or HTTP only on a loopback host.")


def _int(value: object, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LocalModelError(f"{label} must be a whole number.")
    if not low <= value <= high:
        raise LocalModelError(f"{label} must be between {low} and {high}.")
    return value


def _temperature(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalModelError("Temperature must be a number.")
    temperature = float(value)
    if not 0.0 <= temperature <= MAX_TEMPERATURE:
        raise LocalModelError(f"Temperature must be between 0 and {MAX_TEMPERATURE}.")
    return temperature


def create_local_model(
    *,
    label: object,
    kind: object,
    model: object,
    endpoint: object = None,
    num_ctx: object = 16_384,
    num_predict: object = 512,
    temperature: object = 0.0,
    think: object = False,
    created_at: datetime | None = None,
) -> LocalModel:
    """Validate and build a registry entry, deriving its provider id."""
    validated_kind = _kind(kind)
    model_name = _text(model, "Model", MAX_MODEL_NAME_CHARS)
    if _WHITESPACE.search(model_name):
        raise LocalModelError("Model id must not contain whitespace.")
    if not isinstance(think, bool):
        raise LocalModelError("Think must be true or false.")
    return LocalModel(
        id=provider_id(validated_kind, model_name),
        label=_text(label, "Label", MAX_LABEL_CHARS),
        kind=validated_kind,
        model=model_name,
        endpoint=_endpoint(endpoint, validated_kind),
        num_ctx=_int(num_ctx, "Context tokens", MIN_CONTEXT_TOKENS, MAX_CONTEXT_TOKENS),
        num_predict=_int(
            num_predict, "Prediction tokens", MIN_PREDICT_TOKENS, MAX_PREDICT_TOKENS
        ),
        temperature=_temperature(temperature),
        think=think,
        created_at=created_at or datetime.now(UTC),
    )


def _model_from_dict(value: object) -> LocalModel:
    if not isinstance(value, dict):
        raise LocalModelError("A model entry must be an object.")
    built = create_local_model(
        label=value.get("label"),
        kind=value.get("kind"),
        model=value.get("model"),
        endpoint=value.get("endpoint"),
        num_ctx=value.get("num_ctx", 16_384),
        num_predict=value.get("num_predict", 512),
        temperature=value.get("temperature", 0.0),
        think=value.get("think", False),
        created_at=_value_timestamp(value.get("created_at")),
    )
    stored_id = value.get("id")
    if stored_id is not None and stored_id != built.id:
        raise LocalModelError("A model id does not match its backend and model.")
    return built


def _value_timestamp(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise LocalModelError("created_at must be a timestamp.")
    try:
        return core_parse_timestamp(value)
    except ValueError as exc:
        raise LocalModelError("created_at must be a timestamp.") from exc


def parse_local_models(raw: str | None) -> tuple[LocalModel, ...]:
    """Read the stored registry; malformed entries are skipped, not fatal."""
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(payload, list):
        return ()
    models: list[LocalModel] = []
    seen: set[str] = set()
    for item in payload:
        try:
            model = _model_from_dict(item)
        except LocalModelError:
            continue
        if model.id not in seen:
            seen.add(model.id)
            models.append(model)
    return tuple(models)


def serialize_local_models(models: Sequence[LocalModel]) -> str:
    return json.dumps(
        [model.to_dict() for model in models],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def validate_local_models(models: Iterable[LocalModel]) -> tuple[LocalModel, ...]:
    ordered: list[LocalModel] = []
    seen: set[str] = set()
    for model in models:
        if not isinstance(model, LocalModel):
            raise LocalModelError("A model registry entry is required.")
        if model.id in seen:
            raise LocalModelError(f"Duplicate model {model.model!r}.")
        seen.add(model.id)
        ordered.append(model)
    if len(ordered) > MAX_LOCAL_MODELS:
        raise LocalModelError(f"At most {MAX_LOCAL_MODELS} models can be registered.")
    return tuple(ordered)


def seed_from_provider_specs(
    specs: Iterable[tuple[str, str]],
    *,
    default_endpoint: str | None = None,
) -> tuple[LocalModel, ...]:
    """Convert Promptfoo ``(id, label)`` pairs into registry entries."""
    models: list[LocalModel] = []
    for spec_id, label in specs:
        kind: ModelKind = (
            "ollama" if spec_id.startswith("ollama:chat:") else "openai_compatible"
        )
        model = spec_id.split(":", 2)[2] if spec_id.count(":") >= 2 else spec_id
        try:
            models.append(
                create_local_model(
                    label=label,
                    kind=kind,
                    model=model,
                    endpoint=default_endpoint if kind == "openai_compatible" else None,
                )
            )
        except LocalModelError:
            continue
    return validate_local_models(models)


def ollama_available() -> bool:
    return shutil.which("ollama") is not None


def installed_ollama_models(*, timeout: float = 5.0) -> set[str] | None:
    """Names reported by ``ollama list``; ``None`` when it cannot be queried."""
    binary = shutil.which("ollama")
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "list"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    names: set[str] = set()
    for line in completed.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            names.add(parts[0])
    return names


def model_is_installed(model: LocalModel, installed: set[str] | None) -> bool | None:
    """``None`` means unknown (not an Ollama model, or Ollama unavailable)."""
    if model.kind != "ollama" or installed is None:
        return None
    if model.model in installed:
        return True
    return f"{model.model}:latest" in installed


PULL_STATUSES = frozenset(
    {"idle", "queued", "running", "completed", "failed", "cancelled"}
)
PULL_TAIL_LINES = 100


@dataclass(frozen=True, slots=True)
class PullState:
    status: str
    output: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "output": self.output}


class ModelPullError(RuntimeError):
    """A pull could not start (wrong backend or one already running)."""


class ModelPullRunner:
    """Owns the single running ``ollama pull`` process and its bounded output."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._model_id: str | None = None
        self._state = PullState("idle")

    @property
    def active_model_id(self) -> str | None:
        return self._model_id if self.is_running() else None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self, model_id: str) -> PullState | None:
        return self._state if self._model_id == model_id else None

    def start(self, model: LocalModel) -> PullState:
        if model.kind != "ollama":
            raise ModelPullError("Only Ollama models can be pulled here.")
        if self.is_running():
            raise ModelPullError(f"Already pulling {self._model_id}.")
        binary = shutil.which("ollama")
        if binary is None:
            raise ModelPullError("ollama is not on PATH.")
        self._model_id = model.id
        self._state = PullState("queued")
        self._task = asyncio.create_task(self._execute(binary, model))
        return self._state

    async def _execute(self, binary: str, model: LocalModel) -> None:
        output: deque[str] = deque(maxlen=PULL_TAIL_LINES)
        try:
            self._process = await asyncio.create_subprocess_exec(
                binary,
                "pull",
                model.model,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            self._state = PullState("running")
            stream = self._process.stdout
            if stream is not None:
                async for raw in stream:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if not line:
                        continue
                    output.append(line)
                    self._state = PullState("running", "\n".join(output))
            returncode = await self._process.wait()
        except asyncio.CancelledError:
            await self._terminate()
            self._state = PullState("cancelled", "\n".join(output))
            raise
        except Exception as exc:  # noqa: BLE001 - record and stop cleanly
            self._state = PullState("failed", f"{type(exc).__name__}: {exc}")
            return
        finally:
            self._process = None

        if returncode == 0:
            self._state = PullState("completed", "\n".join(output))
        else:
            tail = "\n".join(output).strip()
            self._state = PullState(
                "failed",
                tail or f"ollama pull exited {returncode}",
            )

    async def _terminate(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            signal_group = getattr(os, "killpg", None)
            if signal_group is not None:
                signal_group(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            await process.wait()

    async def cancel(self, model_id: str) -> bool:
        if self._model_id != model_id or self._task is None:
            return False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return True

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None


__all__ = [
    "DEFAULT_OLLAMA_ENDPOINT",
    "LocalModel",
    "LocalModelError",
    "MAX_LOCAL_MODELS",
    "MODEL_KINDS",
    "ModelKind",
    "ModelPullError",
    "ModelPullRunner",
    "PullState",
    "create_local_model",
    "installed_ollama_models",
    "model_is_installed",
    "ollama_available",
    "parse_local_models",
    "provider_id",
    "seed_from_provider_specs",
    "serialize_local_models",
    "validate_local_models",
]
