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
from decimal import Decimal, InvalidOperation
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
KEY_BEDROCK_REGION = "bedrock_region"


class SettingsError(ValueError):
    pass


class Provider(StrEnum):
    GEMINI = "GEMINI"
    DEEPSEEK = "DEEPSEEK"
    OPENAI = "OPENAI"
    CUSTOM = "CUSTOM"
    TYPESAFE_JEV = "TYPESAFE_JEV"
    BEDROCK = "BEDROCK"


DEFAULT_PROVIDER = Provider.GEMINI

KEY_DECISION_ROUTE = "decision_route"
KEY_AGENT_ROUTING = "agent_routing"
KEY_DECISION_POLICY = "decision_policy"
KEY_PIPELINE = "pipeline"
KEY_RISK_LIMITS = "risk_limits"
KEY_CAMPAIGN = "campaign"
KEY_LOGGING = "logging"
KEY_LOCAL_MODELS = "local_models"
KEY_AIHF_SECRETS = "aihf_secrets"
KEY_RESEARCH_LAB = "research_lab"

# Financial Datasets keys are ~40 chars; this is a ceiling, not a format check.
MAX_DATA_KEY_CHARS = 256

# 20 defaults ship (15 + 5 AI Hedge Fund personas), leaving room for an owner's
# own additions; 32 was reached by that growth and would have capped them out.
MAX_AGENT_ROUTING_ENTRIES = 48
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


class EvidenceAuditorMode(StrEnum):
    """How a candidate report is audited before release.

    ``deterministic`` is the default and the only mode that can *admit* or
    *reject* evidence: the gate is code, so a model cannot validate its own
    numbers (arXiv:2611.24845 §3.2.3). ``llm`` additionally lets a model
    propose a repair to a failing draft, but that proposal still has to pass
    the same deterministic check.
    """

    DETERMINISTIC = "deterministic"
    LLM = "llm"


MAX_RESEARCH_ITERATIONS = 50
MAX_RESEARCH_RETRIES = 5
MIN_COMPACTION_TOKENS = 1000
MAX_COMPACTION_TOKENS = 200000
MAX_ALPHA_HORIZON_BARS = 250
MAX_ALPHA_UNIVERSE = 2000
MAX_ALPHA_HORIZONS = 6


@dataclass(frozen=True, slots=True)
class ResearchModelSlot:
    """One configurable model slot in the research harness."""

    tier: TierName
    max_output_tokens: int = 4096
    temperature: float = 0.2


@dataclass(frozen=True, slots=True)
class ResearchTriageConfig:
    """The atomic decision battery used to triage research requests.

    ``tier`` is pinned to ``decision`` because TypeSafe Jev is only valid there
    (see :func:`validate_tier_config`); the owner can disable the battery but
    cannot point it at a text model.
    """

    enabled: bool = True
    tier: TierName = TierName.DECISION


@dataclass(frozen=True, slots=True)
class ResearchBudgets:
    """Loop budgets. All four are bounded in :func:`validate_research_lab_config`."""

    max_iterations: int = 12
    max_recovery_retries: int = 2
    max_revision_retries: int = 2
    compaction_token_threshold: int = 24000


@dataclass(frozen=True, slots=True)
class IndiaCosts:
    """Effective NSE/BSE assumptions recorded in each run's ``theta``.

    Rates are exact ``Decimal`` fractions of notional, never floats: a run's
    recorded cost model must reproduce to the paise. Brokerage defaults to zero
    because this project has no brokerage component today and a guessed
    per-order rate would silently change every backtest; the owner confirms it.
    """

    brokerage_bps: Decimal = Decimal("0")
    brokerage_flat_paise: int = 0
    brokerage_cap_paise: int = 0
    exchange_rate: Decimal = Decimal("0.0000297")
    sebi_rate: Decimal = Decimal("0.000001")
    ipft_rate: Decimal = Decimal("0")
    stt_delivery: Decimal = Decimal("0.001")
    stt_intraday_sell: Decimal = Decimal("0.00025")
    stamp_delivery_buy: Decimal = Decimal("0.00015")
    stamp_intraday_buy: Decimal = Decimal("0.00003")
    gst: Decimal = Decimal("0.18")


@dataclass(frozen=True, slots=True)
class AlphaBenchSettings:
    horizons: tuple[int, ...] = (1, 5, 20)
    max_universe: int = 500
    min_names_per_date: int = 5


@dataclass(frozen=True, slots=True)
class ResearchLabConfig:
    """The Research Lab's four agent slots, budgets and run assumptions.

    Tiers live here; per-agent provider/model overrides live in
    ``agent_routing`` so the existing Settings save path stays the only writer
    of routes. Nothing in this block names a model.
    """

    planner: ResearchModelSlot = ResearchModelSlot(TierName.REASONING, 8192, 0.1)
    synthesizer: ResearchModelSlot = ResearchModelSlot(TierName.REASONING, 8192, 0.1)
    evidence_auditor: ResearchModelSlot = ResearchModelSlot(TierName.FLASH, 2048, 0.0)
    evidence_auditor_mode: EvidenceAuditorMode = EvidenceAuditorMode.DETERMINISTIC
    triage_scorer: ResearchTriageConfig = ResearchTriageConfig()
    budgets: ResearchBudgets = ResearchBudgets()
    costs: IndiaCosts = IndiaCosts()
    alpha_bench: AlphaBenchSettings = AlphaBenchSettings()


DEFAULT_RESEARCH_LAB = ResearchLabConfig()

# The slot key in `research_lab` -> the agent id used for routing, prompts and
# the UI. Kept as one table so all three surfaces cannot drift apart.
RESEARCH_LAB_SLOTS: tuple[tuple[str, str], ...] = (
    ("planner", "research_lab_planner"),
    ("synthesizer", "research_lab_synthesizer"),
    ("evidence_auditor", "research_lab_evidence_auditor"),
    ("triage_scorer", "research_lab_triage_scorer"),
)
RESEARCH_LAB_AGENT_IDS: tuple[str, ...] = tuple(
    agent_id for _, agent_id in RESEARCH_LAB_SLOTS
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
            # AI Hedge Fund personas (ADR-053). Each is its own agent id so the
            # owner can point one persona at a different tier or model from
            # Settings without touching the others.
            "aihf_buffett": AgentRoute(TierName.REASONING),
            "aihf_munger": AgentRoute(TierName.REASONING),
            "aihf_graham": AgentRoute(TierName.REASONING),
            "aihf_lynch": AgentRoute(TierName.REASONING),
            "aihf_druckenmiller": AgentRoute(TierName.REASONING),
            # Research Lab slots (ADR-054). The tier here is only the routing
            # default; `research_lab` owns the authoritative tier for each slot
            # and the provider/model override is the usual per-agent one.
            "research_lab_planner": AgentRoute(TierName.REASONING),
            "research_lab_synthesizer": AgentRoute(TierName.REASONING),
            "research_lab_evidence_auditor": AgentRoute(TierName.FLASH),
            "research_lab_triage_scorer": AgentRoute(TierName.DECISION),
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
    research_lab: ResearchLabConfig = DEFAULT_RESEARCH_LAB
    decision_route: DecisionRoute = DecisionRoute.AUTO
    decision_policy: DecisionPolicyConfig = DecisionPolicyConfig()
    risk_limits: RiskLimits | None = None
    campaign: CampaignConfig = CampaignConfig()
    logging: LoggingConfig = LoggingConfig()
    custom_endpoint: str | None = None
    bedrock_region: str | None = None
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

# A bare AWS region name, e.g. us-east-1 or ap-south-1. Validated because it is
# interpolated into a Bedrock hostname; an arbitrary string would let stored
# configuration choose the host a request is sent to.
_AWS_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}")
DEFAULT_BEDROCK_REGION = "us-east-1"


def validate_bedrock_region(value: str) -> str:
    """A Bedrock region: empty means the default, anything odd is rejected."""
    if not isinstance(value, str):
        raise SettingsError("Bedrock region must be text.")
    region = value.strip().lower()
    if not region:
        return ""
    if len(region) > 32 or _AWS_REGION.fullmatch(region) is None:
        raise SettingsError(
            "Bedrock region must be an AWS region name such as us-east-1."
        )
    return region


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


@dataclass(frozen=True, slots=True)
class AiHfSecrets:
    """The Financial Datasets credential used by the ``aihf`` sidecar.

    Deliberately *not* part of ``AppConfig``: provider keys reach the API
    through ``AppConfigStatus``, and this one must never ride along there.
    Only ``load_aihf_secrets`` (write path) and ``aihf_secrets`` (read path)
    touch it.
    """

    financial_datasets_api_key: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.financial_datasets_api_key)

    @property
    def masked(self) -> str | None:
        if not self.financial_datasets_api_key:
            return None
        return mask_api_key(self.financial_datasets_api_key)

    def to_projection(self) -> dict[str, object]:
        """The only shape allowed to leave the process."""
        return {
            "financial_datasets": {
                "configured": self.configured,
                "masked": self.masked,
            }
        }


def validate_data_api_key(value: str | None) -> str:
    if not isinstance(value, str):
        raise SettingsError("Financial Datasets API key must be text.")
    key = value.strip()
    if not MIN_API_KEY_CHARS <= len(key) <= MAX_DATA_KEY_CHARS:
        raise SettingsError(
            f"Financial Datasets API key must be between {MIN_API_KEY_CHARS} and "
            f"{MAX_DATA_KEY_CHARS} characters."
        )
    if not key.isprintable() or any(char.isspace() for char in key):
        raise SettingsError(
            "Financial Datasets API key must be a single printable token."
        )
    return key


def load_aihf_secrets(raw: str | None) -> AiHfSecrets:
    """Defensive read: a malformed row falls back to "not configured"."""
    if not raw:
        return AiHfSecrets()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return AiHfSecrets()
    if not isinstance(parsed, dict):
        return AiHfSecrets()
    value = parsed.get("financial_datasets_api_key")
    if not isinstance(value, str) or not value.strip():
        return AiHfSecrets()
    return AiHfSecrets(financial_datasets_api_key=value.strip())


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


def _bounded_int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError(f"{name} must be an integer.")
    if not low <= value <= high:
        raise SettingsError(f"{name} must be between {low} and {high}.")
    return value


def _bounded_rate(value: object, name: str, *, high: Decimal = Decimal("1")) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise SettingsError(f"{name} must be a finite Decimal rate.")
    if not Decimal("0") <= value <= high:
        raise SettingsError(f"{name} must be between 0 and {high}.")
    return value


def validate_research_slot(name: str, slot: ResearchModelSlot) -> ResearchModelSlot:
    if not isinstance(slot, ResearchModelSlot):
        raise SettingsError(f"The {name} research slot is required.")
    tier = validate_tier_name(slot.tier)
    if tier is TierName.DECISION:
        raise SettingsError(
            f"The {name} research slot makes text calls and cannot use the decision tier."
        )
    return ResearchModelSlot(
        tier=tier,
        max_output_tokens=_bounded_int(
            slot.max_output_tokens, f"{name} max output tokens", 1, MAX_TOKENS_LIMIT
        ),
        temperature=validate_temperature(slot.temperature),
    )


def validate_india_costs(costs: IndiaCosts) -> IndiaCosts:
    if not isinstance(costs, IndiaCosts):
        raise SettingsError("India cost rates are required.")
    return IndiaCosts(
        brokerage_bps=_bounded_rate(
            costs.brokerage_bps, "Brokerage bps", high=Decimal("1000")
        ),
        brokerage_flat_paise=_bounded_int(
            costs.brokerage_flat_paise,
            "Brokerage flat paise",
            0,
            MAX_ALPHA_HORIZON_BARS * 100000,
        ),
        brokerage_cap_paise=_bounded_int(
            costs.brokerage_cap_paise,
            "Brokerage cap paise",
            0,
            MAX_ALPHA_HORIZON_BARS * 100000,
        ),
        exchange_rate=_bounded_rate(costs.exchange_rate, "Exchange rate"),
        sebi_rate=_bounded_rate(costs.sebi_rate, "SEBI rate"),
        ipft_rate=_bounded_rate(costs.ipft_rate, "IPFT rate"),
        stt_delivery=_bounded_rate(costs.stt_delivery, "STT delivery rate"),
        stt_intraday_sell=_bounded_rate(costs.stt_intraday_sell, "STT intraday rate"),
        stamp_delivery_buy=_bounded_rate(
            costs.stamp_delivery_buy, "Delivery stamp duty"
        ),
        stamp_intraday_buy=_bounded_rate(
            costs.stamp_intraday_buy, "Intraday stamp duty"
        ),
        gst=_bounded_rate(costs.gst, "GST rate"),
    )


def validate_alpha_bench(config: AlphaBenchSettings) -> AlphaBenchSettings:
    if not isinstance(config, AlphaBenchSettings):
        raise SettingsError("The alpha bench settings are required.")
    horizons = config.horizons
    if not isinstance(horizons, tuple) or not 1 <= len(horizons) <= MAX_ALPHA_HORIZONS:
        raise SettingsError(
            f"The alpha bench needs 1 to {MAX_ALPHA_HORIZONS} horizons."
        )
    ordered: list[int] = []
    for horizon in horizons:
        value = _bounded_int(horizon, "An alpha horizon", 1, MAX_ALPHA_HORIZON_BARS)
        if value in ordered:
            raise SettingsError("Alpha horizons must be distinct.")
        ordered.append(value)
    return AlphaBenchSettings(
        horizons=tuple(ordered),
        max_universe=_bounded_int(
            config.max_universe, "Alpha bench universe size", 2, MAX_ALPHA_UNIVERSE
        ),
        min_names_per_date=_bounded_int(
            config.min_names_per_date, "Minimum names per date", 2, MAX_ALPHA_UNIVERSE
        ),
    )


def validate_research_lab_config(config: ResearchLabConfig) -> ResearchLabConfig:
    if not isinstance(config, ResearchLabConfig):
        raise SettingsError("A research lab configuration is required.")
    if not isinstance(config.evidence_auditor_mode, EvidenceAuditorMode):
        raise SettingsError("The evidence auditor mode must be deterministic or llm.")
    triage = config.triage_scorer
    if not isinstance(triage, ResearchTriageConfig):
        raise SettingsError("The research triage configuration is required.")
    if validate_tier_name(triage.tier) is not TierName.DECISION:
        raise SettingsError("Research triage runs on the decision tier only.")
    budgets = config.budgets
    if not isinstance(budgets, ResearchBudgets):
        raise SettingsError("The research budgets are required.")
    return ResearchLabConfig(
        planner=validate_research_slot("planner", config.planner),
        synthesizer=validate_research_slot("synthesizer", config.synthesizer),
        evidence_auditor=validate_research_slot(
            "evidence auditor", config.evidence_auditor
        ),
        evidence_auditor_mode=config.evidence_auditor_mode,
        triage_scorer=ResearchTriageConfig(
            enabled=bool(triage.enabled), tier=TierName.DECISION
        ),
        budgets=ResearchBudgets(
            max_iterations=_bounded_int(
                budgets.max_iterations, "Max iterations", 1, MAX_RESEARCH_ITERATIONS
            ),
            max_recovery_retries=_bounded_int(
                budgets.max_recovery_retries,
                "Max recovery retries",
                0,
                MAX_RESEARCH_RETRIES,
            ),
            max_revision_retries=_bounded_int(
                budgets.max_revision_retries,
                "Max revision retries",
                0,
                MAX_RESEARCH_RETRIES,
            ),
            compaction_token_threshold=_bounded_int(
                budgets.compaction_token_threshold,
                "Compaction token threshold",
                MIN_COMPACTION_TOKENS,
                MAX_COMPACTION_TOKENS,
            ),
        ),
        costs=validate_india_costs(config.costs),
        alpha_bench=validate_alpha_bench(config.alpha_bench),
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
        return validate_agent_routing(_with_default_agents(entries))
    except (ValueError, TypeError):
        return DEFAULT_AGENT_ROUTING


def _with_default_agents(
    stored: dict[str, "AgentRoute"],
) -> dict[str, "AgentRoute"]:
    """The owner's routes, plus any default agent a stored map predates.

    A settings row written before a new agent existed holds only the ids that
    existed then. Without this merge, `route_for("aihf_buffett")` would silently
    fall back to the reasoning tier for every existing owner — a default that
    looks configured but is not the one the code declares, and invisible in the
    UI. Only *missing* ids are added; a stored entry is never overwritten, so an
    owner's tier or model override survives every upgrade.
    """
    merged = dict(stored)
    for agent_id, route in DEFAULT_AGENT_ROUTING:
        merged.setdefault(agent_id, route)
    return merged


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
            research_lab=_load_research_lab(rows),
            decision_route=_load_decision_route(rows),
            decision_policy=_load_decision_policy(rows),
            risk_limits=_load_risk_limits(rows),
            campaign=_load_campaign(rows),
            logging=_load_logging(rows),
            custom_endpoint=custom_endpoint,
            bedrock_region=rows.get(KEY_BEDROCK_REGION) or None,
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

    def save_research_lab(
        self,
        config: ResearchLabConfig,
        *,
        agent_routing: Mapping[str, AgentRoute] | None = None,
    ) -> AppConfig:
        """Store the Research Lab block and, optionally, its agent routes.

        Only the four Research Lab agent ids may be routed here: every other
        route stays owned by ``save_models`` so this method cannot become a
        second way to reconfigure the whole agent fleet.
        """
        validated = validate_research_lab_config(config)
        current = self.load()
        routing = dict(current.agent_routing)
        if agent_routing is not None:
            unknown = set(agent_routing) - set(RESEARCH_LAB_AGENT_IDS)
            if unknown:
                raise SettingsError(
                    "Only Research Lab agents can be routed from the Research Lab "
                    "settings."
                )
            routing.update(agent_routing)
        validated_routing = validate_agent_routing(routing)
        updated_at = _utc(self.clock.now())
        stamp = _timestamp(updated_at)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                for key, value in (
                    (KEY_RESEARCH_LAB, _research_lab_json(validated)),
                    (KEY_AGENT_ROUTING, _agent_routing_json(dict(validated_routing))),
                ):
                    connection.execute(
                        "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(setting_key) DO UPDATE SET "
                        "setting_value = excluded.setting_value, "
                        "updated_at = excluded.updated_at",
                        (key, value, stamp),
                    )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.research_lab.updated",
                    object_type="app_settings",
                    object_id=KEY_RESEARCH_LAB,
                    payload={
                        "evidence_auditor_mode": validated.evidence_auditor_mode.value,
                        "planner_tier": validated.planner.tier.value,
                        "synthesizer_tier": validated.synthesizer.tier.value,
                        "evidence_auditor_tier": validated.evidence_auditor.tier.value,
                        "triage_enabled": validated.triage_scorer.enabled,
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

    def save_bedrock_region(self, region: str) -> AppConfig:
        """Store the Bedrock region; an empty value removes it."""
        value = validate_bedrock_region(region)
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
                        (KEY_BEDROCK_REGION, value, _timestamp(updated_at)),
                    )
                else:
                    connection.execute(
                        "DELETE FROM app_settings WHERE setting_key = ?",
                        (KEY_BEDROCK_REGION,),
                    )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.bedrock_region.updated",
                    object_type="app_settings",
                    object_id=KEY_BEDROCK_REGION,
                    payload={"bedrock_region": value},
                )
        return self.load()

    # ---- AI Hedge Fund sidecar secrets -----------------------------------
    def aihf_secrets(self) -> AiHfSecrets:
        """The Financial Datasets credential, for injecting into a run only.

        Never project this into ``AppConfig``: it is read here and handed
        straight to the scoped environment of a subprocess.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = ?",
                (KEY_AIHF_SECRETS,),
            ).fetchone()
        return load_aihf_secrets(None if row is None else str(row[0]))

    def save_aihf_secrets(
        self, *, financial_datasets_api_key: str | None
    ) -> AiHfSecrets:
        """Store the credential; ``None`` or empty clears it."""
        if financial_datasets_api_key is None or not financial_datasets_api_key.strip():
            return self.clear_aihf_secrets()
        key = validate_data_api_key(financial_datasets_api_key)
        value = json.dumps(
            {"financial_datasets_api_key": key}, sort_keys=True, separators=(",", ":")
        )
        if len(value) > MAX_SETTING_CHARS:
            raise SettingsError(
                "The stored AI Hedge Fund settings exceed the storage limit."
            )
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
                    (KEY_AIHF_SECRETS, value, _timestamp(updated_at)),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.aihf_secrets.updated",
                    object_type="app_settings",
                    object_id=KEY_AIHF_SECRETS,
                    payload={"financial_datasets": mask_api_key(key)},
                )
        return self.aihf_secrets()

    def clear_aihf_secrets(self) -> AiHfSecrets:
        updated_at = _utc(self.clock.now())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "DELETE FROM app_settings WHERE setting_key = ?",
                    (KEY_AIHF_SECRETS,),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="settings.aihf_secrets.cleared",
                    object_type="app_settings",
                    object_id=KEY_AIHF_SECRETS,
                    payload={},
                )
        return AiHfSecrets()


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


def _slot_json(slot: ResearchModelSlot) -> dict[str, object]:
    return {
        "tier": slot.tier.value,
        "max_output_tokens": slot.max_output_tokens,
        "temperature": slot.temperature,
    }


def _research_lab_json(config: ResearchLabConfig) -> str:
    """Serialise the Research Lab block. Decimal rates stay exact as strings."""
    payload: dict[str, object] = {
        "planner": _slot_json(config.planner),
        "synthesizer": _slot_json(config.synthesizer),
        "evidence_auditor": _slot_json(config.evidence_auditor),
        "evidence_auditor_mode": config.evidence_auditor_mode.value,
        "triage_scorer": {
            "enabled": config.triage_scorer.enabled,
            "tier": config.triage_scorer.tier.value,
        },
        "budgets": {
            "max_iterations": config.budgets.max_iterations,
            "max_recovery_retries": config.budgets.max_recovery_retries,
            "max_revision_retries": config.budgets.max_revision_retries,
            "compaction_token_threshold": config.budgets.compaction_token_threshold,
        },
        "costs": {
            "brokerage_bps": str(config.costs.brokerage_bps),
            "brokerage_flat_paise": config.costs.brokerage_flat_paise,
            "brokerage_cap_paise": config.costs.brokerage_cap_paise,
            "exchange_rate": str(config.costs.exchange_rate),
            "sebi_rate": str(config.costs.sebi_rate),
            "ipft_rate": str(config.costs.ipft_rate),
            "stt_delivery": str(config.costs.stt_delivery),
            "stt_intraday_sell": str(config.costs.stt_intraday_sell),
            "stamp_delivery_buy": str(config.costs.stamp_delivery_buy),
            "stamp_intraday_buy": str(config.costs.stamp_intraday_buy),
            "gst": str(config.costs.gst),
        },
        "alpha_bench": {
            "horizons": list(config.alpha_bench.horizons),
            "max_universe": config.alpha_bench.max_universe,
            "min_names_per_date": config.alpha_bench.min_names_per_date,
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _str_or(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _num_or(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _count_or(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _decimal_or(value: object, default: Decimal) -> Decimal:
    if value is None:
        return default
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SettingsError("A stored cost rate is not a decimal number.") from exc
    if not parsed.is_finite():
        raise SettingsError("A stored cost rate must be finite.")
    return parsed


def _slot_from(
    value: object, default: ResearchModelSlot, name: str
) -> ResearchModelSlot:
    if not isinstance(value, dict):
        return default
    try:
        tier = TierName(str(value.get("tier", default.tier.value)))
    except ValueError as exc:
        raise SettingsError(f"The stored {name} tier is not a known tier.") from exc
    return ResearchModelSlot(
        tier=tier,
        max_output_tokens=_count_or(
            value.get("max_output_tokens"), default.max_output_tokens
        ),
        temperature=_num_or(value.get("temperature"), default.temperature),
    )


def _load_research_lab(rows: Mapping[str, str]) -> ResearchLabConfig:
    """Read the Research Lab block, falling back to defaults on any bad row."""
    raw = rows.get(KEY_RESEARCH_LAB)
    if raw is None:
        return DEFAULT_RESEARCH_LAB
    defaults = DEFAULT_RESEARCH_LAB
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return DEFAULT_RESEARCH_LAB
        budgets = _mapping(payload.get("budgets"))
        costs = _mapping(payload.get("costs"))
        bench = _mapping(payload.get("alpha_bench"))
        triage = _mapping(payload.get("triage_scorer"))
        horizons = bench.get("horizons")
        parsed_horizons = (
            tuple(
                item
                for item in horizons
                if isinstance(item, int) and not isinstance(item, bool)
            )
            if isinstance(horizons, list)
            else defaults.alpha_bench.horizons
        )
        try:
            mode = EvidenceAuditorMode(
                str(
                    payload.get(
                        "evidence_auditor_mode", defaults.evidence_auditor_mode.value
                    )
                )
            )
        except ValueError:
            mode = defaults.evidence_auditor_mode
        config = ResearchLabConfig(
            planner=_slot_from(payload.get("planner"), defaults.planner, "planner"),
            synthesizer=_slot_from(
                payload.get("synthesizer"), defaults.synthesizer, "synthesizer"
            ),
            evidence_auditor=_slot_from(
                payload.get("evidence_auditor"),
                defaults.evidence_auditor,
                "evidence auditor",
            ),
            evidence_auditor_mode=mode,
            triage_scorer=ResearchTriageConfig(
                enabled=bool(triage.get("enabled", defaults.triage_scorer.enabled)),
                tier=TierName.DECISION,
            ),
            budgets=ResearchBudgets(
                max_iterations=_count_or(
                    budgets.get("max_iterations"), defaults.budgets.max_iterations
                ),
                max_recovery_retries=_count_or(
                    budgets.get("max_recovery_retries"),
                    defaults.budgets.max_recovery_retries,
                ),
                max_revision_retries=_count_or(
                    budgets.get("max_revision_retries"),
                    defaults.budgets.max_revision_retries,
                ),
                compaction_token_threshold=_count_or(
                    budgets.get("compaction_token_threshold"),
                    defaults.budgets.compaction_token_threshold,
                ),
            ),
            costs=IndiaCosts(
                brokerage_bps=_decimal_or(
                    costs.get("brokerage_bps"), defaults.costs.brokerage_bps
                ),
                brokerage_flat_paise=_count_or(
                    costs.get("brokerage_flat_paise"),
                    defaults.costs.brokerage_flat_paise,
                ),
                brokerage_cap_paise=_count_or(
                    costs.get("brokerage_cap_paise"), defaults.costs.brokerage_cap_paise
                ),
                exchange_rate=_decimal_or(
                    costs.get("exchange_rate"), defaults.costs.exchange_rate
                ),
                sebi_rate=_decimal_or(costs.get("sebi_rate"), defaults.costs.sebi_rate),
                ipft_rate=_decimal_or(costs.get("ipft_rate"), defaults.costs.ipft_rate),
                stt_delivery=_decimal_or(
                    costs.get("stt_delivery"), defaults.costs.stt_delivery
                ),
                stt_intraday_sell=_decimal_or(
                    costs.get("stt_intraday_sell"), defaults.costs.stt_intraday_sell
                ),
                stamp_delivery_buy=_decimal_or(
                    costs.get("stamp_delivery_buy"), defaults.costs.stamp_delivery_buy
                ),
                stamp_intraday_buy=_decimal_or(
                    costs.get("stamp_intraday_buy"), defaults.costs.stamp_intraday_buy
                ),
                gst=_decimal_or(costs.get("gst"), defaults.costs.gst),
            ),
            alpha_bench=AlphaBenchSettings(
                horizons=parsed_horizons,
                max_universe=_count_or(
                    bench.get("max_universe"), defaults.alpha_bench.max_universe
                ),
                min_names_per_date=_count_or(
                    bench.get("min_names_per_date"),
                    defaults.alpha_bench.min_names_per_date,
                ),
            ),
        )
        return validate_research_lab_config(config)
    except (SettingsError, ValueError, TypeError, KeyError):
        return DEFAULT_RESEARCH_LAB


def _require_lab_provider(value: object) -> Provider | None:
    if value is None or value == "":
        return None
    try:
        return Provider(str(value))
    except ValueError as exc:
        raise SettingsError(
            "A Research Lab provider override is not a provider."
        ) from exc


def resolve_research_lab_models(
    config: AppConfig,
    session_overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, AgentRoute]:
    """Resolve the four Research Lab slots, merging in-memory overrides.

    Tiers come from ``research_lab``; provider/model overrides come from the
    stored ``agent_routing`` row; ``session_overrides`` is applied last and is
    never written. Keys may be either a slot name (``planner``) or an agent id
    (``research_lab_planner``).
    """
    lab = config.research_lab
    slot_tiers = {
        "research_lab_planner": lab.planner.tier,
        "research_lab_synthesizer": lab.synthesizer.tier,
        "research_lab_evidence_auditor": lab.evidence_auditor.tier,
        "research_lab_triage_scorer": lab.triage_scorer.tier,
    }
    resolved: dict[str, AgentRoute] = {}
    for agent_id, tier in slot_tiers.items():
        stored = config.route_for(agent_id)
        resolved[agent_id] = AgentRoute(
            tier=tier, provider=stored.provider, model=stored.model
        )
    if not session_overrides:
        return resolved
    slots = dict(RESEARCH_LAB_SLOTS)
    for key, override in session_overrides.items():
        agent_id = key if key in slot_tiers else slots.get(key)
        if agent_id is None:
            raise SettingsError(f"Unknown Research Lab slot: {key}.")
        if not isinstance(override, Mapping):
            raise SettingsError("A Research Lab override must be a mapping.")
        base = resolved[agent_id]
        tier = base.tier
        if override.get("tier") is not None:
            tier = validate_tier_name(str(override["tier"]))
        if agent_id == "research_lab_triage_scorer" and tier is not TierName.DECISION:
            raise SettingsError("Research triage runs on the decision tier only.")
        provider = (
            _require_lab_provider(override["provider"])
            if "provider" in override
            else base.provider
        )
        model = base.model
        if "model" in override:
            raw_model = override["model"]
            model = None if raw_model is None else validate_model_name(str(raw_model))
        resolved[agent_id] = AgentRoute(tier=tier, provider=provider, model=model)
    return resolved


def research_lab_payload(config: AppConfig) -> dict[str, object]:
    """The Research Lab block as the API and UI read it."""
    payload = json.loads(_research_lab_json(config.research_lab))
    return payload if isinstance(payload, dict) else {}


def parse_research_lab(payload: object) -> ResearchLabConfig:
    """Strictly build the block from a request payload.

    Unlike :func:`_load_research_lab`, a bad value raises instead of falling back
    to a default: an API write that silently changed one field would be worse
    than a 422.
    """
    if not isinstance(payload, Mapping):
        raise SettingsError("A research lab payload must be an object.")
    defaults = DEFAULT_RESEARCH_LAB

    def slot(name: str, default: ResearchModelSlot) -> ResearchModelSlot:
        raw = payload.get(name, {})
        if not isinstance(raw, Mapping):
            raise SettingsError(f"The {name} slot must be an object.")
        try:
            tier = TierName(str(raw.get("tier", default.tier.value)))
        except ValueError as exc:
            raise SettingsError(f"The {name} tier is not a known tier.") from exc
        return ResearchModelSlot(
            tier=tier,
            max_output_tokens=raw.get("max_output_tokens", default.max_output_tokens),
            temperature=raw.get("temperature", default.temperature),
        )

    def numbers(section: str) -> Mapping[str, object]:
        raw = payload.get(section, {})
        if not isinstance(raw, Mapping):
            raise SettingsError(f"The {section} section must be an object.")
        return raw

    budgets = numbers("budgets")
    costs = numbers("costs")
    bench = numbers("alpha_bench")
    triage = numbers("triage_scorer")
    try:
        mode = EvidenceAuditorMode(
            str(
                payload.get(
                    "evidence_auditor_mode", defaults.evidence_auditor_mode.value
                )
            )
        )
    except ValueError as exc:
        raise SettingsError("The evidence auditor mode is unknown.") from exc
    horizons = bench.get("horizons", list(defaults.alpha_bench.horizons))
    if not isinstance(horizons, list):
        raise SettingsError("Alpha horizons must be a list.")
    return validate_research_lab_config(
        ResearchLabConfig(
            planner=slot("planner", defaults.planner),
            synthesizer=slot("synthesizer", defaults.synthesizer),
            evidence_auditor=slot("evidence_auditor", defaults.evidence_auditor),
            evidence_auditor_mode=mode,
            triage_scorer=ResearchTriageConfig(
                enabled=bool(triage.get("enabled", defaults.triage_scorer.enabled)),
                tier=TierName.DECISION,
            ),
            budgets=ResearchBudgets(
                max_iterations=budgets.get(
                    "max_iterations", defaults.budgets.max_iterations
                ),
                max_recovery_retries=budgets.get(
                    "max_recovery_retries", defaults.budgets.max_recovery_retries
                ),
                max_revision_retries=budgets.get(
                    "max_revision_retries", defaults.budgets.max_revision_retries
                ),
                compaction_token_threshold=budgets.get(
                    "compaction_token_threshold",
                    defaults.budgets.compaction_token_threshold,
                ),
            ),
            costs=IndiaCosts(
                brokerage_bps=_decimal_or(
                    costs.get("brokerage_bps"), defaults.costs.brokerage_bps
                ),
                brokerage_flat_paise=costs.get(
                    "brokerage_flat_paise", defaults.costs.brokerage_flat_paise
                ),
                brokerage_cap_paise=costs.get(
                    "brokerage_cap_paise", defaults.costs.brokerage_cap_paise
                ),
                exchange_rate=_decimal_or(
                    costs.get("exchange_rate"), defaults.costs.exchange_rate
                ),
                sebi_rate=_decimal_or(costs.get("sebi_rate"), defaults.costs.sebi_rate),
                ipft_rate=_decimal_or(costs.get("ipft_rate"), defaults.costs.ipft_rate),
                stt_delivery=_decimal_or(
                    costs.get("stt_delivery"), defaults.costs.stt_delivery
                ),
                stt_intraday_sell=_decimal_or(
                    costs.get("stt_intraday_sell"), defaults.costs.stt_intraday_sell
                ),
                stamp_delivery_buy=_decimal_or(
                    costs.get("stamp_delivery_buy"), defaults.costs.stamp_delivery_buy
                ),
                stamp_intraday_buy=_decimal_or(
                    costs.get("stamp_intraday_buy"), defaults.costs.stamp_intraday_buy
                ),
                gst=_decimal_or(costs.get("gst"), defaults.costs.gst),
            ),
            alpha_bench=AlphaBenchSettings(
                horizons=tuple(horizons),
                max_universe=bench.get(
                    "max_universe", defaults.alpha_bench.max_universe
                ),
                min_names_per_date=bench.get(
                    "min_names_per_date", defaults.alpha_bench.min_names_per_date
                ),
            ),
        )
    )
