"""Owner-managed application configuration.

Non-secret values live in ``app_settings``; provider API keys live in
``provider_api_keys``. This module is the only writer of either table.

Two deliberate limits:

* Every value is bounded before it is stored, so a typo cannot inflate a row and
  every read stays cheap.
* API keys are never returned in plaintext by ``load``. It reports only whether
  a key exists and a masked hint. The key itself is stored in plaintext in the
  local database, which is acceptable for a single-owner, loopback-only
  application but is not vault-grade protection.
"""

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from bq.campaign_config import CampaignConfig, CampaignConfigError
from bq.core import (
    Clock,
    SystemClock,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    parse_timestamp as core_parse_timestamp,
)
from bq.core import (
    utc as core_utc,
)
from bq.infra.audit import append_event
from bq.local_models import (
    LocalModel,
    LocalModelError,
    parse_local_models,
    serialize_local_models,
    validate_local_models,
)
from bq.logbook import LoggingConfig, LoggingConfigError
from bq.news import DEFAULT_MEMORY_TTL, PULSE_FEED_URL
from bq.risk.policy import RiskLimits


def _utc(value: datetime) -> datetime:
    try:
        return core_utc(value)
    except ValueError as exc:
        raise SettingsError("Configuration timestamps must be timezone-aware.") from exc


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return core_parse_timestamp(value)


DEFAULT_NEWS_MEMORY_TTL_DAYS = DEFAULT_MEMORY_TTL.days
MAX_MODEL_NAME_CHARS = 200
MIN_MEMORY_TTL_DAYS = 1
MAX_MEMORY_TTL_DAYS = 3650
MAX_API_KEY_CHARS = 2048
MIN_API_KEY_CHARS = 8
MAX_SETTING_CHARS = 4096
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

KEY_PROVIDER = "provider"
KEY_MODEL_NAME = "model_name"
KEY_NEWS_FEED_URL = "news_feed_url"
KEY_NEWS_MEMORY_TTL_DAYS = "news_memory_ttl_days"
KEY_KITE_ENABLED = "kite_enabled"
KEY_CUSTOM_ENDPOINT = "custom_endpoint"


class SettingsError(ValueError):
    pass


class Provider(StrEnum):
    GEMINI = "GEMINI"
    DEEPSEEK = "DEEPSEEK"
    OPENAI = "OPENAI"
    CUSTOM = "CUSTOM"
    TYPESAFE_JEV = "TYPESAFE_JEV"


DEFAULT_PROVIDER = Provider.GEMINI

KEY_DECISION_ROUTE = "decision_route"
KEY_AGENT_ROUTING = "agent_routing"
KEY_DECISION_POLICY = "decision_policy"
KEY_PIPELINE = "pipeline"
KEY_RISK_LIMITS = "risk_limits"
KEY_CAMPAIGN = "campaign"
KEY_LOGGING = "logging"
KEY_LOCAL_MODELS = "local_models"

MAX_AGENT_ROUTING_ENTRIES = 32
MAX_AGENT_ID_CHARS = 64
MAX_TEMPERATURE = 2.0
MAX_TOKENS_LIMIT = 32768
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 60000
MAX_ATTEMPTS = 3
MAX_DEBATE_ROUNDS = 3
MAX_TOOL_ROUNDS = 20
DEFAULT_DECISION_MODEL = "jev-1.14.0"
DEFAULT_DECISION_TIMEOUT_MS = 3000
DEFAULT_TEXT_TIMEOUT_MS = 60000
_AGENT_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_UNIMPLEMENTED_DECISION_ROUTES = frozenset({"openrouter_jev", "requesty_jev"})


class TierName(StrEnum):
    DECISION = "decision"
    REASONING = "reasoning"
    FLASH = "flash"


class DecisionRoute(StrEnum):
    AUTO = "auto"
    TYPESAFE_JEV = "typesafe_jev"
    LLM_STRUCTURED_FALLBACK = "llm_structured_fallback"
    OPENROUTER_JEV = "openrouter_jev"
    REQUESTY_JEV = "requesty_jev"


class DecisionFallback(StrEnum):
    FLASH = "flash"
    REASONING = "reasoning"
    HEURISTIC = "heuristic"


_TIER_ORDER = {TierName.DECISION: 0, TierName.FLASH: 1, TierName.REASONING: 2}


@dataclass(frozen=True, slots=True)
class TierConfig:
    provider: Provider | None
    model: str
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout_ms: int = DEFAULT_TEXT_TIMEOUT_MS
    max_attempts: int = 3
    fallback_tier: TierName | None = None

    @property
    def configured(self) -> bool:
        return self.provider is not None and bool(self.model)


@dataclass(frozen=True, slots=True)
class AgentRoute:
    tier: TierName
    provider: Provider | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionPolicyConfig:
    min_choice_confidence: float = 0.55
    min_noul_probability: float = 0.60
    abstain_score_band: tuple[int, int] = (2, 3)
    llm_fallback_size_scale: float = 0.75
    heuristic_size_scale: float = 0.50


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    debate_rounds: int = 1
    max_tool_rounds_per_agent: int = 8
    screener_min_score: float = 0.60
    final_decision_min_prob: float = 0.65
    execution_mode: str = "PAPER"
    decision_fallback_order: tuple[DecisionFallback, ...] = (
        DecisionFallback.FLASH,
        DecisionFallback.REASONING,
        DecisionFallback.HEURISTIC,
    )


DEFAULT_AGENT_ROUTING: tuple[tuple[str, AgentRoute], ...] = tuple(
    sorted(
        {
            "watchlist_screener": AgentRoute(TierName.DECISION),
            "news_triage": AgentRoute(TierName.DECISION),
            "evidence_verifier": AgentRoute(TierName.DECISION),
            "final_decision_gate": AgentRoute(TierName.DECISION),
            "bull_debater": AgentRoute(TierName.REASONING),
            "bear_debater": AgentRoute(TierName.REASONING),
            "research_judge": AgentRoute(TierName.REASONING),
            "super_agent_cio": AgentRoute(TierName.REASONING),
            "reflection_writer": AgentRoute(TierName.REASONING),
            "news_research": AgentRoute(TierName.FLASH),
            "research": AgentRoute(TierName.FLASH),
            "fundamental_events": AgentRoute(TierName.FLASH),
            "market_regime": AgentRoute(TierName.FLASH),
            "technical_analyst": AgentRoute(TierName.FLASH),
            "risk_analyst": AgentRoute(TierName.FLASH),
        }.items()
    )
)


@dataclass(frozen=True, slots=True)
class ApiKeyStatus:
    provider: Provider
    configured: bool
    masked: str | None


@dataclass(frozen=True, slots=True)
class AppConfig:
    provider: Provider
    model_name: str
    news_feed_url: str
    news_memory_ttl_days: int
    api_keys: tuple[ApiKeyStatus, ...]
    updated_at: datetime | None
    kite_enabled: bool = False
    tiers: tuple[tuple[TierName, TierConfig], ...] = ()
    agent_routing: tuple[tuple[str, AgentRoute], ...] = ()
    pipeline: PipelineConfig = PipelineConfig()
    decision_route: DecisionRoute = DecisionRoute.AUTO
    decision_policy: DecisionPolicyConfig = DecisionPolicyConfig()
    risk_limits: RiskLimits | None = None
    campaign: CampaignConfig = CampaignConfig()
    logging: LoggingConfig = LoggingConfig()
    custom_endpoint: str | None = None
    local_models: tuple[LocalModel, ...] = ()
    local_models_configured: bool = False

    def tier(self, name: TierName) -> TierConfig | None:
        for key, value in self.tiers:
            if key is name:
                return value
        return None

    def _has_key(self, provider: Provider | None) -> bool:
        if provider is None:
            return False
        if provider is Provider.CUSTOM and self.custom_endpoint:
            return True
        return any(key.provider is provider and key.configured for key in self.api_keys)

    def tier_configured(self, name: TierName) -> bool:
        tier = self.tier(name)
        if tier is None or not tier.configured:
            return False
        return self._has_key(tier.provider)

    @property
    def configured_tiers(self) -> tuple[TierName, ...]:
        return tuple(
            name
            for name in (TierName.DECISION, TierName.REASONING, TierName.FLASH)
            if self.tier_configured(name)
        )

    @property
    def any_tier_configured(self) -> bool:
        return bool(self.configured_tiers)

    def route_for(self, agent_id: str) -> AgentRoute:
        for key, route in self.agent_routing:
            if key == agent_id:
                return route
        return AgentRoute(TierName.REASONING)

    @property
    def decision_preview(self) -> str:
        """Which source the next decision call will try first."""
        if self.decision_route in (DecisionRoute.AUTO, DecisionRoute.TYPESAFE_JEV):
            decision = self.tier(TierName.DECISION)
            if (
                decision is not None
                and decision.model
                and self._has_key(decision.provider)
            ):
                return "jev"
        for fallback in self.pipeline.decision_fallback_order:
            if fallback is DecisionFallback.HEURISTIC:
                return "heuristic"
            if self.tier_configured(TierName(fallback.value)):
                return fallback.value
        return "rules_only"

    @property
    def model_route_configured(self) -> bool:
        """True only when a model name and the matching provider key exist."""
        if not self.model_name:
            return False
        return any(
            key.provider is self.provider and key.configured for key in self.api_keys
        )

    @property
    def api_keys_configured(self) -> bool:
        return any(key.configured for key in self.api_keys)


def _require_provider(provider: Provider | str) -> Provider:
    try:
        return provider if isinstance(provider, Provider) else Provider(provider)
    except ValueError as exc:
        raise SettingsError("Unknown model provider.") from exc


def validate_model_name(value: str) -> str:
    if not isinstance(value, str):
        raise SettingsError("Model name must be text.")
    name = value.strip()
    if len(name) > MAX_MODEL_NAME_CHARS:
        raise SettingsError(
            f"Model name must be at most {MAX_MODEL_NAME_CHARS} characters."
        )
    if _CONTROL.search(name):
        raise SettingsError("Model name must not contain control characters.")
    return name


def validate_news_feed_url(value: str) -> str:
    if not isinstance(value, str):
        raise SettingsError("Feed URL must be text.")
    url = value.strip()
    if len(url) > MAX_SETTING_CHARS:
        raise SettingsError("Feed URL is too long.")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise SettingsError("Feed URL must be HTTPS without embedded credentials.")
    return url


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def validate_custom_endpoint(value: str) -> str:
    """A local model endpoint: https anywhere, plain http only on loopback."""
    if not isinstance(value, str):
        raise SettingsError("Custom endpoint must be text.")
    url = value.strip()
    if not url:
        return ""
    if len(url) > MAX_SETTING_CHARS or _CONTROL.search(url):
        raise SettingsError("Custom endpoint is malformed.")
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise SettingsError("Custom endpoint must not embed credentials.")
    if parts.scheme == "https":
        if not parts.hostname:
            raise SettingsError("Custom endpoint must name a host.")
        return url
    if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
        return url
    raise SettingsError(
        "Custom endpoint must use HTTPS, or HTTP only on a loopback host."
    )


def validate_memory_ttl_days(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError("Memory retention must be a whole number of days.")
    if not MIN_MEMORY_TTL_DAYS <= value <= MAX_MEMORY_TTL_DAYS:
        raise SettingsError(
            f"Memory retention must be between {MIN_MEMORY_TTL_DAYS} and "
            f"{MAX_MEMORY_TTL_DAYS} days."
        )
    return value


def validate_api_key(value: str) -> str:
    if not isinstance(value, str):
        raise SettingsError("API key must be text.")
    key = value.strip()
    if not MIN_API_KEY_CHARS <= len(key) <= MAX_API_KEY_CHARS:
        raise SettingsError(
            f"API key must be between {MIN_API_KEY_CHARS} and "
            f"{MAX_API_KEY_CHARS} characters."
        )
    if not key.isprintable() or any(char.isspace() for char in key):
        raise SettingsError("API key must be a single printable token.")
    return key


def mask_api_key(value: str) -> str:
    """Show enough to recognise the key, never enough to use it."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{'*' * 8}{value[-4:]}"


def validate_temperature(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError("Temperature must be a number.")
    number = float(value)
    if not 0.0 <= number <= MAX_TEMPERATURE:
        raise SettingsError(f"Temperature must be between 0 and {MAX_TEMPERATURE}.")
    return number


def validate_max_tokens(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError("Max tokens must be a whole number.")
    if not 1 <= value <= MAX_TOKENS_LIMIT:
        raise SettingsError(f"Max tokens must be between 1 and {MAX_TOKENS_LIMIT}.")
    return value


def validate_timeout_ms(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError("Timeout must be a whole number of milliseconds.")
    if not MIN_TIMEOUT_MS <= value <= MAX_TIMEOUT_MS:
        raise SettingsError(
            f"Timeout must be between {MIN_TIMEOUT_MS} and {MAX_TIMEOUT_MS} ms."
        )
    return value


def validate_attempts(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError("Attempts must be a whole number.")
    if not 1 <= value <= MAX_ATTEMPTS:
        raise SettingsError(f"Attempts must be between 1 and {MAX_ATTEMPTS}.")
    return value


def validate_probability(value: float, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{label} must be a number.")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise SettingsError(f"{label} must be between 0 and 1.")
    return number


def validate_debate_rounds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError("Debate rounds must be a whole number.")
    if not 1 <= value <= MAX_DEBATE_ROUNDS:
        raise SettingsError(f"Debate rounds must be between 1 and {MAX_DEBATE_ROUNDS}.")
    return value


def validate_tool_rounds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError("Tool rounds must be a whole number.")
    if not 1 <= value <= MAX_TOOL_ROUNDS:
        raise SettingsError(f"Tool rounds must be between 1 and {MAX_TOOL_ROUNDS}.")
    return value


def validate_execution_mode(value: str) -> str:
    if not isinstance(value, str) or value not in {"PAPER", "SANDBOX", "LIVE_APPROVAL"}:
        raise SettingsError("Execution mode must be PAPER, SANDBOX or LIVE_APPROVAL.")
    return value


def validate_tier_name(value: TierName | str) -> TierName:
    try:
        return value if isinstance(value, TierName) else TierName(value)
    except ValueError as exc:
        raise SettingsError("Unknown model tier.") from exc


def validate_decision_route(value: DecisionRoute | str) -> DecisionRoute:
    try:
        route = value if isinstance(value, DecisionRoute) else DecisionRoute(value)
    except ValueError as exc:
        raise SettingsError("Unknown decision route.") from exc
    if route.value in _UNIMPLEMENTED_DECISION_ROUTES:
        raise SettingsError(
            "That Jev route is not implemented; use auto, typesafe_jev or "
            "llm_structured_fallback."
        )
    return route


def validate_fallback_order(
    value: Sequence[DecisionFallback | str],
) -> tuple[DecisionFallback, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise SettingsError("The decision fallback order must be a list.")
    if not 1 <= len(value) <= MAX_ATTEMPTS:
        raise SettingsError(f"The fallback order needs 1 to {MAX_ATTEMPTS} entries.")
    order: list[DecisionFallback] = []
    for item in value:
        try:
            fallback = (
                item if isinstance(item, DecisionFallback) else DecisionFallback(item)
            )
        except ValueError as exc:
            raise SettingsError("Unknown decision fallback.") from exc
        if fallback in order:
            raise SettingsError("The fallback order must not repeat a source.")
        order.append(fallback)
    return tuple(order)


def validate_decision_policy(config: DecisionPolicyConfig) -> DecisionPolicyConfig:
    if not isinstance(config, DecisionPolicyConfig):
        raise SettingsError("A decision policy is required.")
    low, high = config.abstain_score_band
    if (
        isinstance(low, bool)
        or isinstance(high, bool)
        or not isinstance(low, int)
        or not isinstance(high, int)
        or not 0 <= low <= high <= 4
    ):
        raise SettingsError("The abstain score band must be within 0..4 and ordered.")
    return DecisionPolicyConfig(
        min_choice_confidence=validate_probability(
            config.min_choice_confidence, label="Minimum choice confidence"
        ),
        min_noul_probability=validate_probability(
            config.min_noul_probability, label="Minimum noul probability"
        ),
        abstain_score_band=(low, high),
        llm_fallback_size_scale=validate_probability(
            config.llm_fallback_size_scale, label="LLM fallback size scale"
        ),
        heuristic_size_scale=validate_probability(
            config.heuristic_size_scale, label="Heuristic size scale"
        ),
    )


def validate_tier_config(name: TierName, config: TierConfig) -> TierConfig:
    if not isinstance(config, TierConfig):
        raise SettingsError("A tier configuration is required.")
    if name is TierName.DECISION:
        if config.provider not in (None, Provider.TYPESAFE_JEV):
            raise SettingsError("The decision tier only accepts TypeSafe Jev.")
    elif config.provider is Provider.TYPESAFE_JEV:
        raise SettingsError("TypeSafe Jev is only valid for the decision tier.")
    fallback: TierName | None = None
    if config.fallback_tier is not None:
        fallback = validate_tier_name(config.fallback_tier)
        if _TIER_ORDER[fallback] <= _TIER_ORDER[name]:
            raise SettingsError("Fallback tiers must move down the ladder.")
    return TierConfig(
        provider=config.provider,
        model=validate_model_name(config.model),
        temperature=validate_temperature(config.temperature),
        max_tokens=validate_max_tokens(config.max_tokens),
        timeout_ms=validate_timeout_ms(config.timeout_ms),
        max_attempts=validate_attempts(config.max_attempts),
        fallback_tier=fallback,
    )


def validate_agent_routing(
    entries: Mapping[str, AgentRoute],
) -> tuple[tuple[str, AgentRoute], ...]:
    if not isinstance(entries, Mapping):
        raise SettingsError("Agent routing must be a mapping.")
    if not 1 <= len(entries) <= MAX_AGENT_ROUTING_ENTRIES:
        raise SettingsError(
            f"Agent routing needs 1 to {MAX_AGENT_ROUTING_ENTRIES} entries."
        )
    validated: list[tuple[str, AgentRoute]] = []
    for agent_id, route in sorted(entries.items()):
        if not isinstance(agent_id, str) or _AGENT_ID.fullmatch(agent_id) is None:
            raise SettingsError("An agent id must be a short snake_case identifier.")
        if not isinstance(route, AgentRoute):
            raise SettingsError("An agent route is required.")
        tier = validate_tier_name(route.tier)
        provider = route.provider
        if provider is not None and not isinstance(provider, Provider):
            raise SettingsError("An agent provider override must be a provider.")
        if provider is Provider.TYPESAFE_JEV:
            raise SettingsError("TypeSafe Jev is only valid for the decision tier.")
        model = None if route.model is None else validate_model_name(route.model)
        validated.append(
            (agent_id, AgentRoute(tier=tier, provider=provider, model=model))
        )
    return tuple(validated)


def validate_pipeline(config: PipelineConfig) -> PipelineConfig:
    if not isinstance(config, PipelineConfig):
        raise SettingsError("A pipeline configuration is required.")
    return PipelineConfig(
        debate_rounds=validate_debate_rounds(config.debate_rounds),
        max_tool_rounds_per_agent=validate_tool_rounds(
            config.max_tool_rounds_per_agent
        ),
        screener_min_score=validate_probability(
            config.screener_min_score, label="Screener minimum score"
        ),
        final_decision_min_prob=validate_probability(
            config.final_decision_min_prob, label="Final decision minimum probability"
        ),
        execution_mode=validate_execution_mode(config.execution_mode),
        decision_fallback_order=validate_fallback_order(config.decision_fallback_order),
    )


def _safe_model(value: object, default: str) -> str:
    if not isinstance(value, str):
        return default
    text = value.strip()
    if len(text) > MAX_MODEL_NAME_CHARS or _CONTROL.search(text):
        return default
    return text


def _optional_provider(value: object) -> Provider | None:
    if value is None or value == "":
        return None
    try:
        return Provider(str(value))
    except ValueError:
        return None


def _provider_or(value: object, default: Provider) -> Provider:
    parsed = _optional_provider(value)
    return default if parsed is None else parsed


def _row_int(
    rows: Mapping[str, str], key: str, default: int, *, low: int, high: int
) -> int:
    raw = rows.get(key)
    if raw is None:
        return default
    try:
        value = int(str(raw))
    except ValueError:
        return default
    return value if low <= value <= high else default


def _row_float(
    rows: Mapping[str, str], key: str, default: float, *, low: float, high: float
) -> float:
    raw = rows.get(key)
    if raw is None:
        return default
    try:
        value = float(str(raw))
    except ValueError:
        return default
    return value if low <= value <= high else default


def _safe_fallback(
    value: object, name: TierName, default: TierName | None
) -> TierName | None:
    if value is None or value == "":
        return default
    try:
        fallback = TierName(str(value))
    except ValueError:
        return default
    return fallback if _TIER_ORDER[fallback] > _TIER_ORDER[name] else default


def _load_tiers(
    rows: Mapping[str, str], legacy_provider: Provider, legacy_model: str
) -> tuple[tuple[TierName, TierConfig], ...]:
    decision_provider = (
        _optional_provider(rows.get("tier.decision.provider"))
        if "tier.decision.provider" in rows
        else Provider.TYPESAFE_JEV
    )
    decision = TierConfig(
        provider=decision_provider,
        model=_safe_model(rows.get("tier.decision.model"), DEFAULT_DECISION_MODEL),
        temperature=0.0,
        max_tokens=1,
        timeout_ms=_row_int(
            rows,
            "tier.decision.timeout_ms",
            DEFAULT_DECISION_TIMEOUT_MS,
            low=MIN_TIMEOUT_MS,
            high=MAX_TIMEOUT_MS,
        ),
        max_attempts=_row_int(
            rows, "tier.decision.max_attempts", 1, low=1, high=MAX_ATTEMPTS
        ),
        fallback_tier=_safe_fallback(
            rows.get("tier.decision.fallback_tier"), TierName.DECISION, TierName.FLASH
        ),
    )
    reasoning = TierConfig(
        provider=_provider_or(rows.get("tier.reasoning.provider"), legacy_provider),
        model=_safe_model(rows.get("tier.reasoning.model"), legacy_model),
        temperature=_row_float(
            rows, "tier.reasoning.temperature", 0.2, low=0.0, high=MAX_TEMPERATURE
        ),
        max_tokens=_row_int(
            rows, "tier.reasoning.max_tokens", 4096, low=1, high=MAX_TOKENS_LIMIT
        ),
        timeout_ms=_row_int(
            rows,
            "tier.reasoning.timeout_ms",
            DEFAULT_TEXT_TIMEOUT_MS,
            low=MIN_TIMEOUT_MS,
            high=MAX_TIMEOUT_MS,
        ),
        max_attempts=_row_int(
            rows, "tier.reasoning.max_attempts", 3, low=1, high=MAX_ATTEMPTS
        ),
        fallback_tier=_safe_fallback(
            rows.get("tier.reasoning.fallback_tier"), TierName.REASONING, None
        ),
    )
    flash = TierConfig(
        provider=_provider_or(rows.get("tier.flash.provider"), legacy_provider),
        model=_safe_model(rows.get("tier.flash.model"), legacy_model),
        temperature=_row_float(
            rows, "tier.flash.temperature", 0.1, low=0.0, high=MAX_TEMPERATURE
        ),
        max_tokens=_row_int(
            rows, "tier.flash.max_tokens", 2048, low=1, high=MAX_TOKENS_LIMIT
        ),
        timeout_ms=_row_int(
            rows,
            "tier.flash.timeout_ms",
            DEFAULT_TEXT_TIMEOUT_MS,
            low=MIN_TIMEOUT_MS,
            high=MAX_TIMEOUT_MS,
        ),
        max_attempts=_row_int(
            rows, "tier.flash.max_attempts", 3, low=1, high=MAX_ATTEMPTS
        ),
        fallback_tier=_safe_fallback(
            rows.get("tier.flash.fallback_tier"), TierName.FLASH, TierName.REASONING
        ),
    )
    return (
        (TierName.DECISION, decision),
        (TierName.REASONING, reasoning),
        (TierName.FLASH, flash),
    )


def _load_agent_routing(
    rows: Mapping[str, str],
) -> tuple[tuple[str, AgentRoute], ...]:
    raw = rows.get(KEY_AGENT_ROUTING)
    if raw is None:
        return DEFAULT_AGENT_ROUTING
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return DEFAULT_AGENT_ROUTING
        entries: dict[str, AgentRoute] = {}
        for agent_id, value in payload.items():
            if not isinstance(agent_id, str) or not isinstance(value, dict):
                return DEFAULT_AGENT_ROUTING
            tier = TierName(str(value.get("tier")))
            provider = _optional_provider(value.get("provider"))
            model = value.get("model")
            if model is not None and not isinstance(model, str):
                return DEFAULT_AGENT_ROUTING
            entries[agent_id] = AgentRoute(tier=tier, provider=provider, model=model)
        return validate_agent_routing(entries)
    except (ValueError, TypeError):
        return DEFAULT_AGENT_ROUTING


def _load_decision_route(rows: Mapping[str, str]) -> DecisionRoute:
    raw = rows.get(KEY_DECISION_ROUTE)
    try:
        route = DecisionRoute(str(raw)) if raw else DecisionRoute.AUTO
    except ValueError:
        return DecisionRoute.AUTO
    if route.value in _UNIMPLEMENTED_DECISION_ROUTES:
        return DecisionRoute.AUTO
    return route


def _load_decision_policy(rows: Mapping[str, str]) -> DecisionPolicyConfig:
    band: tuple[int, int] = (2, 3)
    raw = rows.get(f"{KEY_DECISION_POLICY}.abstain_score_band")
    if raw is not None:
        try:
            parsed = json.loads(raw)
            if (
                isinstance(parsed, list)
                and len(parsed) == 2
                and all(
                    isinstance(item, int) and not isinstance(item, bool)
                    for item in parsed
                )
            ):
                band = (int(parsed[0]), int(parsed[1]))
        except ValueError:
            pass
    default = DecisionPolicyConfig()
    try:
        return validate_decision_policy(
            DecisionPolicyConfig(
                min_choice_confidence=_row_float(
                    rows,
                    f"{KEY_DECISION_POLICY}.min_choice_confidence",
                    default.min_choice_confidence,
                    low=0.0,
                    high=1.0,
                ),
                min_noul_probability=_row_float(
                    rows,
                    f"{KEY_DECISION_POLICY}.min_noul_probability",
                    default.min_noul_probability,
                    low=0.0,
                    high=1.0,
                ),
                abstain_score_band=band,
                llm_fallback_size_scale=_row_float(
                    rows,
                    f"{KEY_DECISION_POLICY}.llm_fallback_size_scale",
                    default.llm_fallback_size_scale,
                    low=0.0,
                    high=1.0,
                ),
                heuristic_size_scale=_row_float(
                    rows,
                    f"{KEY_DECISION_POLICY}.heuristic_size_scale",
                    default.heuristic_size_scale,
                    low=0.0,
                    high=1.0,
                ),
            )
        )
    except ValueError:
        return default


def _load_pipeline(rows: Mapping[str, str]) -> PipelineConfig:
    default = PipelineConfig()
    order = default.decision_fallback_order
    raw = rows.get(f"{KEY_PIPELINE}.decision_fallback_order")
    if raw is not None:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                order = validate_fallback_order(parsed)
        except ValueError:
            order = default.decision_fallback_order
    try:
        return validate_pipeline(
            PipelineConfig(
                debate_rounds=_row_int(
                    rows,
                    f"{KEY_PIPELINE}.debate_rounds",
                    default.debate_rounds,
                    low=1,
                    high=MAX_DEBATE_ROUNDS,
                ),
                max_tool_rounds_per_agent=_row_int(
                    rows,
                    f"{KEY_PIPELINE}.max_tool_rounds_per_agent",
                    default.max_tool_rounds_per_agent,
                    low=1,
                    high=MAX_TOOL_ROUNDS,
                ),
                screener_min_score=_row_float(
                    rows,
                    f"{KEY_PIPELINE}.screener_min_score",
                    default.screener_min_score,
                    low=0.0,
                    high=1.0,
                ),
                final_decision_min_prob=_row_float(
                    rows,
                    f"{KEY_PIPELINE}.final_decision_min_prob",
                    default.final_decision_min_prob,
                    low=0.0,
                    high=1.0,
                ),
                execution_mode=_safe_execution_mode(
                    rows.get(f"{KEY_PIPELINE}.execution_mode"), default.execution_mode
                ),
                decision_fallback_order=order,
            )
        )
    except ValueError:
        return default


def _load_risk_limits(rows: Mapping[str, str]) -> RiskLimits | None:
    raw = rows.get(KEY_RISK_LIMITS)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    fields = (
        "max_symbol_exposure_paise",
        "max_open_positions",
        "max_daily_loss_paise",
        "max_weekly_loss_paise",
        "max_monthly_loss_paise",
        "max_order_rate_per_minute",
    )
    values: dict[str, int] = {}
    for field in fields:
        value = parsed.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values[field] = value
    try:
        return RiskLimits(**values)
    except (TypeError, ValueError):
        return None


def _load_campaign(rows: Mapping[str, str]) -> CampaignConfig:
    raw = rows.get(KEY_CAMPAIGN)
    if raw is None:
        return CampaignConfig()
    try:
        return CampaignConfig.from_mapping(json.loads(raw))
    except (CampaignConfigError, TypeError, ValueError):
        return CampaignConfig()


def _load_logging(rows: Mapping[str, str]) -> LoggingConfig:
    raw = rows.get(KEY_LOGGING)
    if raw is None:
        return LoggingConfig()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return LoggingConfig()
    if not isinstance(parsed, dict):
        return LoggingConfig()
    try:
        return LoggingConfig.from_mapping(parsed)
    except LoggingConfigError:
        return LoggingConfig()


def _safe_execution_mode(value: object, default: str) -> str:
    if not isinstance(value, str) or value not in {"PAPER", "SANDBOX", "LIVE_APPROVAL"}:
        return default
    return value


class SettingsStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=False)

    def load(self) -> AppConfig:
        with closing(self._connect()) as connection:
            rows = dict(
                connection.execute(
                    "SELECT setting_key, setting_value FROM app_settings"
                ).fetchall()
            )
            key_rows = connection.execute(
                "SELECT provider, api_key, updated_at FROM provider_api_keys"
            ).fetchall()
            latest = connection.execute(
                "SELECT MAX(updated_at) FROM ("
                "SELECT updated_at FROM app_settings "
                "UNION ALL SELECT updated_at FROM provider_api_keys)"
            ).fetchone()
        provider = DEFAULT_PROVIDER
        if KEY_PROVIDER in rows:
            try:
                provider = Provider(str(rows[KEY_PROVIDER]))
            except ValueError:
                provider = DEFAULT_PROVIDER
        ttl = DEFAULT_NEWS_MEMORY_TTL_DAYS
        if KEY_NEWS_MEMORY_TTL_DAYS in rows:
            try:
                ttl = int(str(rows[KEY_NEWS_MEMORY_TTL_DAYS]))
            except ValueError:
                ttl = DEFAULT_NEWS_MEMORY_TTL_DAYS
        updated = None
        if latest is not None and latest[0] is not None:
            updated = _parse_timestamp(str(latest[0]))
        # Absent means off: a fresh install must not attempt broker calls.
        kite_enabled = str(rows.get(KEY_KITE_ENABLED, "false")).lower() == "true"
        model_name = str(rows.get(KEY_MODEL_NAME, ""))
        custom_endpoint = rows.get(KEY_CUSTOM_ENDPOINT) or None
        return AppConfig(
            provider=provider,
            model_name=model_name,
            news_feed_url=str(rows.get(KEY_NEWS_FEED_URL, PULSE_FEED_URL)),
            news_memory_ttl_days=ttl,
            api_keys=tuple(
                ApiKeyStatus(
                    provider=Provider(str(row[0])),
                    configured=True,
                    masked=mask_api_key(str(row[1])),
                )
                for row in sorted(key_rows, key=lambda item: str(item[0]))
            ),
            updated_at=updated,
            kite_enabled=kite_enabled,
            tiers=_load_tiers(rows, provider, model_name),
            agent_routing=_load_agent_routing(rows),
            pipeline=_load_pipeline(rows),
            decision_route=_load_decision_route(rows),
            decision_policy=_load_decision_policy(rows),
            risk_limits=_load_risk_limits(rows),
            campaign=_load_campaign(rows),
            logging=_load_logging(rows),
            custom_endpoint=custom_endpoint,
            local_models=parse_local_models(rows.get(KEY_LOCAL_MODELS)),
            local_models_configured=KEY_LOCAL_MODELS in rows,
        )

    def api_key(self, provider: Provider | str) -> str | None:
        """Return the stored key for internal clients. Never expose via the API."""
        target = _require_provider(provider)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT api_key FROM provider_api_keys WHERE provider = ?",
                (target.value,),
            ).fetchone()
        return None if row is None else str(row[0])

    def save_config(
        self,
        *,
        provider: Provider | str,
        model_name: str,
        news_feed_url: str,
        news_memory_ttl_days: int,
    ) -> AppConfig:
        target = _require_provider(provider)
        if target is Provider.TYPESAFE_JEV:
            raise SettingsError(
                "TypeSafe Jev is configured under Models, not the legacy provider field."
            )
        name = validate_model_name(model_name)
        feed_url = validate_news_feed_url(news_feed_url)
        ttl = validate_memory_ttl_days(news_memory_ttl_days)
        updated_at = _utc(self.clock.now())
        values = {
            KEY_PROVIDER: target.value,
            KEY_MODEL_NAME: name,
            KEY_NEWS_FEED_URL: feed_url,
            KEY_NEWS_MEMORY_TTL_DAYS: str(ttl),
        }
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                for key, value in values.items():
                    connection.execute(
                        "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(setting_key) DO UPDATE SET "
                        "setting_value = excluded.setting_value, "
                        "updated_at = excluded.updated_at",
                        (key, value, _timestamp(updated_at)),
                    )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.config.updated",
                    object_type="app_settings",
                    object_id="config",
                    payload={
                        "provider": target.value,
                        "model_name": name,
                        "news_feed_url": feed_url,
                        "news_memory_ttl_days": str(ttl),
                    },
                )
        return self.load()

    def save_risk_limits(self, limits: RiskLimits) -> AppConfig:
        if not isinstance(limits, RiskLimits):
            raise SettingsError("Risk limits are required.")
        updated_at = _utc(self.clock.now())
        value = json.dumps(
            {
                "max_symbol_exposure_paise": limits.max_symbol_exposure_paise,
                "max_open_positions": limits.max_open_positions,
                "max_daily_loss_paise": limits.max_daily_loss_paise,
                "max_weekly_loss_paise": limits.max_weekly_loss_paise,
                "max_monthly_loss_paise": limits.max_monthly_loss_paise,
                "max_order_rate_per_minute": limits.max_order_rate_per_minute,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(setting_key) DO UPDATE SET "
                    "setting_value = excluded.setting_value, "
                    "updated_at = excluded.updated_at",
                    (KEY_RISK_LIMITS, value, _timestamp(updated_at)),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.risk_limits.updated",
                    object_type="app_settings",
                    object_id=KEY_RISK_LIMITS,
                    payload=json.loads(value),
                )
        return self.load()

    def clear_risk_limits(self) -> AppConfig:
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "DELETE FROM app_settings WHERE setting_key = ?",
                    (KEY_RISK_LIMITS,),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.risk_limits.cleared",
                    object_type="app_settings",
                    object_id=KEY_RISK_LIMITS,
                    payload={},
                )
        return self.load()

    def save_campaign(self, config: CampaignConfig) -> AppConfig:
        if not isinstance(config, CampaignConfig):
            raise SettingsError("Campaign settings are required.")
        value = json.dumps(config.to_dict(), sort_keys=True, separators=(",", ":"))
        if len(value) > MAX_SETTING_CHARS:
            raise SettingsError("Campaign settings exceed the storage limit.")
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(setting_key) DO UPDATE SET "
                    "setting_value = excluded.setting_value, "
                    "updated_at = excluded.updated_at",
                    (KEY_CAMPAIGN, value, _timestamp(updated_at)),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.campaign.updated",
                    object_type="app_settings",
                    object_id=KEY_CAMPAIGN,
                    payload=json.loads(value),
                )
        return self.load()

    def clear_campaign(self) -> AppConfig:
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "DELETE FROM app_settings WHERE setting_key = ?",
                    (KEY_CAMPAIGN,),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.campaign.cleared",
                    object_type="app_settings",
                    object_id=KEY_CAMPAIGN,
                    payload={},
                )
        return self.load()

    def save_logging(self, config: LoggingConfig) -> AppConfig:
        if not isinstance(config, LoggingConfig):
            raise SettingsError("A logging configuration is required.")
        try:
            validated = LoggingConfig.from_mapping(config.to_dict())
        except LoggingConfigError as exc:
            raise SettingsError(str(exc)) from exc
        value = json.dumps(validated.to_dict(), sort_keys=True, separators=(",", ":"))
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(setting_key) DO UPDATE SET "
                    "setting_value = excluded.setting_value, "
                    "updated_at = excluded.updated_at",
                    (KEY_LOGGING, value, _timestamp(updated_at)),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.logging.updated",
                    object_type="app_settings",
                    object_id=KEY_LOGGING,
                    payload=json.loads(value),
                )
        return self.load()

    def clear_logging(self) -> AppConfig:
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "DELETE FROM app_settings WHERE setting_key = ?",
                    (KEY_LOGGING,),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.logging.cleared",
                    object_type="app_settings",
                    object_id=KEY_LOGGING,
                    payload={},
                )
        return self.load()

    def set_api_key(self, provider: Provider | str, api_key: str) -> None:
        target = _require_provider(provider)
        key = validate_api_key(api_key)
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO provider_api_keys (provider, api_key, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(provider) DO UPDATE SET "
                    "api_key = excluded.api_key, updated_at = excluded.updated_at",
                    (target.value, key, _timestamp(updated_at)),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.api_key.set",
                    object_type="provider_api_key",
                    object_id=target.value,
                    payload={"masked": mask_api_key(key)},
                )

    def clear_api_key(self, provider: Provider | str) -> None:
        target = _require_provider(provider)
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "DELETE FROM provider_api_keys WHERE provider = ?",
                    (target.value,),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.api_key.cleared",
                    object_type="provider_api_key",
                    object_id=target.value,
                    payload={},
                )

    def save_models(
        self,
        *,
        tiers: Mapping[TierName, TierConfig],
        agent_routing: Mapping[str, AgentRoute],
        pipeline: PipelineConfig,
        decision_route: DecisionRoute | str,
        decision_policy: DecisionPolicyConfig,
    ) -> AppConfig:
        """Validate and store the three tiers, routing, policy and pipeline."""
        if set(tiers) != {TierName.DECISION, TierName.REASONING, TierName.FLASH}:
            raise SettingsError(
                "Exactly the decision, reasoning and flash tiers are required."
            )
        validated_tiers = {
            name: validate_tier_config(name, tiers[name])
            for name in (TierName.DECISION, TierName.REASONING, TierName.FLASH)
        }
        validated_routing = validate_agent_routing(agent_routing)
        validated_pipeline = validate_pipeline(pipeline)
        validated_route = validate_decision_route(decision_route)
        validated_policy = validate_decision_policy(decision_policy)

        updated_at = _utc(self.clock.now())
        values: dict[str, str] = {}
        for name, config in validated_tiers.items():
            prefix = f"tier.{name.value}."
            values[prefix + "provider"] = (
                "" if config.provider is None else config.provider.value
            )
            values[prefix + "model"] = config.model
            values[prefix + "temperature"] = repr(config.temperature)
            values[prefix + "max_tokens"] = str(config.max_tokens)
            values[prefix + "timeout_ms"] = str(config.timeout_ms)
            values[prefix + "max_attempts"] = str(config.max_attempts)
            values[prefix + "fallback_tier"] = (
                "" if config.fallback_tier is None else config.fallback_tier.value
            )
        values[KEY_DECISION_ROUTE] = validated_route.value
        values[KEY_AGENT_ROUTING] = _agent_routing_json(dict(validated_routing))
        policy_prefix = KEY_DECISION_POLICY + "."
        values[policy_prefix + "min_choice_confidence"] = repr(
            validated_policy.min_choice_confidence
        )
        values[policy_prefix + "min_noul_probability"] = repr(
            validated_policy.min_noul_probability
        )
        values[policy_prefix + "abstain_score_band"] = json.dumps(
            list(validated_policy.abstain_score_band), separators=(",", ":")
        )
        values[policy_prefix + "llm_fallback_size_scale"] = repr(
            validated_policy.llm_fallback_size_scale
        )
        values[policy_prefix + "heuristic_size_scale"] = repr(
            validated_policy.heuristic_size_scale
        )
        pipeline_prefix = KEY_PIPELINE + "."
        values[pipeline_prefix + "debate_rounds"] = str(
            validated_pipeline.debate_rounds
        )
        values[pipeline_prefix + "max_tool_rounds_per_agent"] = str(
            validated_pipeline.max_tool_rounds_per_agent
        )
        values[pipeline_prefix + "screener_min_score"] = repr(
            validated_pipeline.screener_min_score
        )
        values[pipeline_prefix + "final_decision_min_prob"] = repr(
            validated_pipeline.final_decision_min_prob
        )
        values[pipeline_prefix + "execution_mode"] = validated_pipeline.execution_mode
        values[pipeline_prefix + "decision_fallback_order"] = json.dumps(
            [item.value for item in validated_pipeline.decision_fallback_order],
            separators=(",", ":"),
        )

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                for key, value in values.items():
                    connection.execute(
                        "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(setting_key) DO UPDATE SET "
                        "setting_value = excluded.setting_value, "
                        "updated_at = excluded.updated_at",
                        (key, value, _timestamp(updated_at)),
                    )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.models.updated",
                    object_type="app_settings",
                    object_id="models",
                    payload={
                        "decision_route": validated_route.value,
                        "decision_model": validated_tiers[TierName.DECISION].model,
                        "reasoning_model": validated_tiers[TierName.REASONING].model,
                        "flash_model": validated_tiers[TierName.FLASH].model,
                    },
                )
        return self.load()

    def save_custom_endpoint(self, endpoint: str) -> AppConfig:
        """Store (or clear) the local OpenAI-compatible endpoint for CUSTOM."""
        value = validate_custom_endpoint(endpoint)
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                if value:
                    connection.execute(
                        "INSERT INTO app_settings "
                        "(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(setting_key) DO UPDATE SET "
                        "setting_value = excluded.setting_value, "
                        "updated_at = excluded.updated_at",
                        (KEY_CUSTOM_ENDPOINT, value, _timestamp(updated_at)),
                    )
                else:
                    connection.execute(
                        "DELETE FROM app_settings WHERE setting_key = ?",
                        (KEY_CUSTOM_ENDPOINT,),
                    )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.custom_endpoint.updated",
                    object_type="app_settings",
                    object_id=KEY_CUSTOM_ENDPOINT,
                    payload={"custom_endpoint": value},
                )
        return self.load()

    def save_local_models(self, models: Sequence[LocalModel]) -> AppConfig:
        """Replace the local model registry."""
        try:
            validated = validate_local_models(models)
        except LocalModelError as exc:
            raise SettingsError(str(exc)) from exc
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(setting_key) DO UPDATE SET "
                    "setting_value = excluded.setting_value, "
                    "updated_at = excluded.updated_at",
                    (
                        KEY_LOCAL_MODELS,
                        serialize_local_models(validated),
                        _timestamp(updated_at),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.local_models.updated",
                    object_type="app_settings",
                    object_id=KEY_LOCAL_MODELS,
                    payload={"models": ",".join(model.id for model in validated)},
                )
        return self.load()

    def save_agent_model(
        self, *, agent_id: str, provider: Provider | str, model: str
    ) -> AppConfig:
        """Point one agent role at a specific provider/model, keeping the rest."""
        target = _require_provider(provider)
        if _AGENT_ID.fullmatch(agent_id) is None:
            raise SettingsError("An agent id must be a short snake_case identifier.")
        if target is Provider.TYPESAFE_JEV:
            raise SettingsError("TypeSafe Jev is only valid for the decision tier.")
        config = self.load()
        routing = dict(config.agent_routing)
        existing = routing.get(agent_id) or AgentRoute(TierName.REASONING)
        routing[agent_id] = AgentRoute(
            tier=existing.tier, provider=target, model=validate_model_name(model)
        )
        return self.save_models(
            tiers=dict(config.tiers),
            agent_routing=routing,
            pipeline=config.pipeline,
            decision_route=config.decision_route,
            decision_policy=config.decision_policy,
        )


def _agent_routing_json(entries: Mapping[str, AgentRoute]) -> str:
    payload: dict[str, object] = {}
    for agent_id, route in sorted(entries.items()):
        item: dict[str, object] = {"tier": route.tier.value}
        if route.provider is not None:
            item["provider"] = route.provider.value
        if route.model is not None:
            item["model"] = route.model
        payload[agent_id] = item
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
