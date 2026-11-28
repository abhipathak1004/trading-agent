"""Build the environment ``aihf`` expects, from BharatQuant's Settings.

``aihf`` reads its keys from the environment (its ``.env`` file is loaded with
``override=False``, so an injected variable always wins — see
``hedge_fund/tui/keys.py``). That makes this module the single source of
truth: a key present in Settings is injected, and a key absent from Settings
is **removed** from the inherited environment, so a stray shell export or an
old ``~/.hedge-fund/.env`` can never silently run.

Nothing here ever mutates ``os.environ``, and nothing here ever logs or
returns a key in plaintext.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from bq.aihf.config import AiHfPaths
from bq.settings import Provider, SettingsStore

# aihf's provider name -> the environment variable it reads (llm/registry.py).
AIHF_ENV_VARS: dict[str, str] = {
    "Anthropic": "ANTHROPIC_API_KEY",
    "OpenAI": "OPENAI_API_KEY",
    "xAI": "XAI_API_KEY",
    "DeepSeek": "DEEPSEEK_API_KEY",
    "Google": "GOOGLE_API_KEY",
    "Kimi": "KIMI_API_KEY",
    "TypeSafe": "TYPESAFE_API_KEY",
}

# The only bridges available: BharatQuant stores no Anthropic/xAI/Kimi key.
BQ_TO_AIHF_PROVIDER: dict[Provider, str] = {
    Provider.GEMINI: "Google",
    Provider.OPENAI: "OpenAI",
    Provider.DEEPSEEK: "DeepSeek",
    Provider.TYPESAFE_JEV: "TypeSafe",
}

DATA_KEY_ENV = "FINANCIAL_DATASETS_API_KEY"
WALLET_KEY_ENV = "BASE_WALLET_KEY"
MODEL_ENV = "HEDGE_FUND_LLM_MODEL"

# Every variable this module owns: removed from the inherited environment
# before anything is injected, so the result is a pure function of Settings.
OWNED_VARS: tuple[str, ...] = (
    *AIHF_ENV_VARS.values(),
    DATA_KEY_ENV,
    WALLET_KEY_ENV,
    MODEL_ENV,
)


@dataclass(frozen=True, slots=True)
class AiHfRuntime:
    """What a run will use. Carries no key material — only which vars were set."""

    env: dict[str, str]
    provider: str | None = None
    model: str | None = None
    configured_vars: tuple[str, ...] = ()
    missing: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> dict[str, object]:
        """A safe projection for status endpoints and logs."""
        return {
            "provider": self.provider,
            "model": self.model,
            "configured_vars": list(self.configured_vars),
            "missing": list(self.missing),
            "has_llm_key": self.provider is not None,
            "has_data_key": DATA_KEY_ENV in self.configured_vars,
        }


def _provider_keys(settings: SettingsStore) -> dict[str, str]:
    """aihf provider name -> key, for every configured provider we can bridge."""
    keys: dict[str, str] = {}
    for provider, name in BQ_TO_AIHF_PROVIDER.items():
        value = settings.api_key(provider)
        if value:
            keys[name] = value
    return keys


def _clean_base(environ: Mapping[str, str] | None) -> dict[str, str]:
    """The inherited environment with every variable this module owns removed."""
    base = dict(environ if environ is not None else {})
    for name in OWNED_VARS:
        base.pop(name, None)
    return base


def resolve_model(
    model_override: str | None, paths: AiHfPaths | None
) -> tuple[str | None, str | None]:
    """The aihf model id and the provider that serves it.

    ``aihf`` derives the provider *from the model id* through its own registry
    and silently falls back to Anthropic for an id it does not know, so an
    override we cannot route is refused rather than run on the wrong vendor.
    """
    model = (model_override or "").strip() or None
    if model is None:
        return None, None
    if paths is None:
        raise ValueError(
            "Cannot resolve a model override without the aihf sidecar paths."
        )
    from bq.aihf.registry import provider_for  # local import: probe-backed

    provider = provider_for(model, paths)
    if provider is None:
        raise ValueError(
            f"aihf does not know the model {model!r}; pick one from its registry "
            "so the request is not silently sent to Anthropic"
        )
    return model, provider


def get_aihf_runtime_env(
    settings: SettingsStore,
    *,
    model_override: str | None = None,
    environ: Mapping[str, str] | None = None,
    paths: AiHfPaths,
) -> dict[str, str]:
    """The exact environment for one ``aihf`` execution. Never touches ``os.environ``.

    Sorted, plain ``str`` values so it is safe to hand to ``subprocess``.
    """
    return build_runtime(
        settings, model_override=model_override, environ=environ, paths=paths
    ).env


def build_runtime(
    settings: SettingsStore,
    *,
    model_override: str | None = None,
    environ: Mapping[str, str] | None = None,
    paths: AiHfPaths,
) -> AiHfRuntime:
    """``get_aihf_runtime_env`` plus the metadata the UI and logs need."""
    base = _clean_base(environ)
    keys = _provider_keys(settings)
    for provider_name, value in keys.items():
        base[AIHF_ENV_VARS[provider_name]] = value

    configured: list[str] = []
    missing: list[str] = list(AIHF_ENV_VARS.values())

    secrets = settings.aihf_secrets()
    if secrets.financial_datasets_api_key:
        base[DATA_KEY_ENV] = secrets.financial_datasets_api_key
        configured.append(DATA_KEY_ENV)

    if paths is not None:
        # Every run is confined to this project's own aihf home, so the
        # real ~/.hedge-fund (and its .env) is never read or written.
        paths.home.mkdir(parents=True, exist_ok=True)
        base["HOME"] = str(paths.home)

    model, provider = resolve_model(model_override, paths)
    if model is None:
        _, provider = _active_bridge(settings, keys)
    else:
        base[MODEL_ENV] = model

    if provider is not None:
        var = AIHF_ENV_VARS[provider]
        if var in base:
            configured.append(var)
            missing = [name for name in missing if name != var]

    return AiHfRuntime(
        env={str(k): str(v) for k, v in sorted(base.items())},
        provider=provider,
        model=model,
        configured_vars=tuple(configured),
        missing=tuple(missing),
    )


def _active_bridge(
    settings: SettingsStore, keys: Mapping[str, str]
) -> tuple[str | None, str | None]:
    """The BharatQuant-selected provider, if it can be bridged to aihf.

    Returns ``(bq provider value, aihf provider name)``; ``(None, None)`` when
    the selected provider has no aihf counterpart (e.g. ``CUSTOM``, local).
    """
    selected = settings.load().provider
    name = BQ_TO_AIHF_PROVIDER.get(selected)
    if name is None or name not in keys:
        return None, None
    return selected.value, name
