"""What a run will use, projected safely — with no key material.

Upstream this module built the *environment* a subprocess would be handed: it
chose a provider, found a matching api-key variable, and injected it into a
scoped ``env`` dict. The engine runs in-process now (ADR-053), so there is no
environment to build: a key is read from ``SettingsStore`` at the call site, by
``ModelRouter``, exactly like every other agent in this app.

What survives is the projection the status card needs — which provider, which
model, whether an LLM is configured at all — carrying **no key**. That is still
the right shape: the UI must be able to say "no provider configured" without ever
holding a secret.
"""

from __future__ import annotations

from dataclasses import dataclass

from bq.settings import Provider, SettingsStore

# The provider names the engine's registry used, and the ones this app can
# actually serve. Kept for the status card's "missing" list, which is what an
# owner reads to know what to configure.
PROVIDER_ENV_VARS: dict[str, str] = {
    "Anthropic": "ANTHROPIC_API_KEY",
    "OpenAI": "OPENAI_API_KEY",
    "xAI": "XAI_API_KEY",
    "DeepSeek": "DEEPSEEK_API_KEY",
    "Google": "GOOGLE_API_KEY",
    "Kimi": "KIMI_API_KEY",
    "TypeSafe": "TYPESAFE_API_KEY",
}

# This app's providers, by the name the engine used. Only these can be served:
# a persona routed to anything else would have no key and no client.
BQ_TO_AIHF_PROVIDER: dict[Provider, str] = {
    Provider.GEMINI: "Google",
    Provider.OPENAI: "OpenAI",
    Provider.DEEPSEEK: "DeepSeek",
    Provider.TYPESAFE_JEV: "TypeSafe",
    Provider.CUSTOM: "Local",
    Provider.BEDROCK: "Bedrock",
}


@dataclass(frozen=True, slots=True)
class AiHfRuntime:
    """What a run will use. Carries no key material — only what is configured."""

    provider: str | None = None
    model: str | None = None
    configured_vars: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    def describe(self) -> dict[str, object]:
        """A safe projection for status endpoints and logs."""
        return {
            "provider": self.provider,
            "model": self.model,
            "configured_vars": list(self.configured_vars),
            "missing": list(self.missing),
            "has_llm_key": self.provider is not None,
        }


def build_runtime(settings: SettingsStore) -> AiHfRuntime:
    """Project which provider this app can serve a persona with.

    A provider counts as configured when it has a key (or is a keyless local
    endpoint, which `SettingsStore.api_key` reports as ``"local"``). The active
    provider is the one selected in Settings, which is also what a persona's
    route resolves through.
    """
    config = settings.load()
    selected = config.provider
    name = BQ_TO_AIHF_PROVIDER.get(selected)
    configured: list[str] = []
    if name is not None:
        var = PROVIDER_ENV_VARS.get(name)
        key = settings.api_key(selected)
        if key is not None and var is not None:
            configured.append(var)
    missing = tuple(
        var for provider_name, var in PROVIDER_ENV_VARS.items() if var not in configured
    )
    return AiHfRuntime(
        provider=name if configured else None,
        model=None,
        configured_vars=tuple(configured),
        missing=missing,
    )


__all__ = [
    "AiHfRuntime",
    "BQ_TO_AIHF_PROVIDER",
    "PROVIDER_ENV_VARS",
    "build_runtime",
]
