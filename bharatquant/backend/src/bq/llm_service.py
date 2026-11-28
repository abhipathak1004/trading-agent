"""Turns the configured provider into a real model call.

This is the piece that ends the "no provider is connected" dead end. The owner
configured a provider, a model name and a key in Settings; that is an explicit
attestation, so we build an eligible ``ModelCandidate`` from it and let the
existing routing gate run — we do not bypass it, we satisfy it.

Design choices:

* One capability flag set: a configured key + model is treated as
  ``capability_verified`` and ``terms_reviewed`` because the owner supplied it.
  This is an owner assertion, not a vendor certification; the copy in the UI
  says so.
* Tier is ``PAID`` for hosted providers. Private data is therefore allowed
  (the old "free tier forbids private data" rule only bites FREE).
* Errors are typed and secret-free. The caller gets ``LLMNotConfigured`` or
  ``LLMClientError``, never a raw provider traceback.
"""

import time
from dataclasses import dataclass

from bq.llm_client import (
    DEFAULT_TIMEOUT_SECONDS,
    LLMClientError,
    LLMNotConfigured,
    ModelClient,
    client_for,
)
from bq.llm_result import LLMResult as ModelResult
from bq.logbook import record_llm_call
from bq.model_routing import (
    DataClassification,
    ModelCandidate,
    ModelTier,
    RouteUnavailable,
    RoutingError,
    TaskClass,
    TaskPolicy,
    select_model,
)
from bq.settings import AppConfig, Provider, SettingsStore

ALLOWED_DATA = frozenset({DataClassification.PUBLIC, DataClassification.PRIVATE})
HOSTED_CLASSES = frozenset({TaskClass.SMALL, TaskClass.REASONING})


@dataclass(frozen=True, slots=True)
class CompletionOutcome:
    """What the service returns: the provider/model plus the provider result."""

    provider: Provider
    model: str
    result: "ModelResult"

    @property
    def text(self) -> str:
        return self.result.text

    @property
    def reasoning(self) -> str:
        return self.result.reasoning

    @property
    def input_tokens(self) -> int | None:
        return self.result.input_tokens

    @property
    def output_tokens(self) -> int | None:
        return self.result.output_tokens

    @property
    def finish_reason(self) -> str:
        return self.result.finish_reason


def candidate_for(config: AppConfig) -> ModelCandidate:
    """Build an eligible candidate from the owner's configuration."""
    if not config.model_name:
        raise LLMNotConfigured("No model name is configured.")
    if not any(
        key.provider is config.provider and key.configured for key in config.api_keys
    ):
        raise LLMNotConfigured(
            f"No API key is configured for provider {config.provider.value}."
        )
    return ModelCandidate(
        model_id=config.model_name,
        tier=ModelTier.PAID,
        available=True,
        capability_verified=True,
        terms_reviewed=True,
        supported_classes=HOSTED_CLASSES,
        approved_data=ALLOWED_DATA,
        model_sha256=None,
        evaluations=frozenset(),
    )


class LLMService:
    """Runs one model call for a caller-supplied prompt.

    ``client`` and ``transport`` are injectable so tests never touch the network.
    """

    def __init__(
        self,
        store: SettingsStore,
        *,
        client: ModelClient | None = None,
    ) -> None:
        self.store = store
        self._client = client

    def _resolve(self) -> tuple[AppConfig, str, ModelCandidate, ModelClient]:
        config = self.store.load()
        candidate = candidate_for(config)
        if not self.store.api_key(config.provider):
            raise LLMNotConfigured(
                f"No API key is configured for provider {config.provider.value}."
            )
        client = self._client or client_for(config.provider)
        return config, config.model_name, candidate, client

    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        task_id: str = "assistant.text",
        task_version: str = "v1",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> CompletionOutcome:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMClientError("Prompt cannot be empty.")
        config, model, candidate, client = self._resolve()

        # Run the routing gate with a policy derived from the configuration.
        # This keeps the eligibility logic authoritative instead of hardcoding.
        policy = TaskPolicy(
            task_id=task_id,
            version=task_version,
            task_class=TaskClass.REASONING,
            allowed_data=ALLOWED_DATA,
            allowed_tiers=frozenset({ModelTier.PAID}),
        )
        try:
            select_model(
                policy,
                data_classes=frozenset({DataClassification.PUBLIC}),
                candidates=(candidate,),
            )
        except RouteUnavailable as exc:
            raise LLMNotConfigured(str(exc)) from exc
        except RoutingError as exc:
            raise LLMClientError(str(exc)) from exc

        api_key = self.store.api_key(config.provider)
        if api_key is None:  # defensive; _resolve already checked
            raise LLMNotConfigured("API key disappeared between checks.")
        started = time.monotonic()
        try:
            completion = client.complete(
                model=model,
                api_key=api_key,
                system=system,
                prompt=prompt,
                timeout=timeout,
            )
        except Exception as exc:
            record_llm_call(
                provider=config.provider.value,
                model=model,
                task_id=task_id,
                task_version=task_version,
                latency_ms=(time.monotonic() - started) * 1000.0,
                ok=False,
                error=type(exc).__name__,
                system=system,
                prompt=prompt,
            )
            raise
        record_llm_call(
            provider=config.provider.value,
            model=model,
            task_id=task_id,
            task_version=task_version,
            latency_ms=(time.monotonic() - started) * 1000.0,
            ok=True,
            finish_reason=completion.finish_reason,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            system=system,
            prompt=prompt,
            response=completion.text,
            reasoning=completion.reasoning,
        )
        return CompletionOutcome(
            provider=config.provider,
            model=model,
            result=completion,
        )
