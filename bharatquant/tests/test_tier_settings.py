from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.settings import (
    DEFAULT_AGENT_ROUTING,
    AgentRoute,
    DecisionFallback,
    DecisionPolicyConfig,
    DecisionRoute,
    PipelineConfig,
    Provider,
    SettingsError,
    SettingsStore,
    TierConfig,
    TierName,
    validate_agent_routing,
    validate_fallback_order,
    validate_tier_config,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _store(tmp_path: Path) -> SettingsStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return SettingsStore(path, FrozenClock())


def _tiers() -> dict[TierName, TierConfig]:
    return {
        TierName.DECISION: TierConfig(
            provider=Provider.TYPESAFE_JEV,
            model="jev-1.14.0",
            temperature=0.0,
            max_tokens=1,
            timeout_ms=3000,
            max_attempts=1,
            fallback_tier=TierName.FLASH,
        ),
        TierName.REASONING: TierConfig(
            provider=Provider.GEMINI,
            model="gemini-2.5-pro",
            fallback_tier=None,
        ),
        TierName.FLASH: TierConfig(
            provider=Provider.GEMINI,
            model="gemini-2.5-flash",
            fallback_tier=TierName.REASONING,
        ),
    }


def _save(
    store: SettingsStore, *, decision_provider: Provider | None = Provider.TYPESAFE_JEV
) -> None:
    tiers = _tiers()
    tiers[TierName.DECISION] = TierConfig(
        provider=decision_provider,
        model="jev-1.14.0",
        temperature=0.0,
        max_tokens=1,
        timeout_ms=3000,
        max_attempts=1,
        fallback_tier=TierName.FLASH,
    )
    store.save_models(
        tiers=tiers,
        agent_routing=dict(DEFAULT_AGENT_ROUTING),
        pipeline=PipelineConfig(),
        decision_route=DecisionRoute.AUTO,
        decision_policy=DecisionPolicyConfig(),
    )


def test_defaults_keep_the_legacy_route_and_preview_heuristic(tmp_path: Path) -> None:
    config = _store(tmp_path).load()
    assert config.model_route_configured is False
    assert config.any_tier_configured is False
    assert config.tier(TierName.DECISION) is not None
    assert config.tier(TierName.DECISION).model == "jev-1.14.0"  # type: ignore[union-attr]
    assert config.decision_preview == "heuristic"
    assert config.route_for("bull_debater").tier is TierName.REASONING
    assert config.route_for("unknown_agent").tier is TierName.REASONING


def test_legacy_gemini_config_maps_to_text_tiers_and_flash_preview(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.save_config(
        provider=Provider.GEMINI,
        model_name="gemini-2.5-pro",
        news_feed_url="https://example.com/feed.xml",
        news_memory_ttl_days=30,
    )
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-key-1234")
    config = store.load()
    assert config.model_route_configured is True
    assert config.tier_configured(TierName.REASONING) is True
    assert config.tier_configured(TierName.FLASH) is True
    assert config.tier_configured(TierName.DECISION) is False
    assert config.decision_preview == "flash"


def test_models_round_trip_and_jev_preview(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store)
    store.set_api_key(Provider.TYPESAFE_JEV, "jev-synthetic-key-1234")
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-key-1234")
    config = store.load()
    assert config.decision_route is DecisionRoute.AUTO
    assert config.tier(TierName.FLASH) is not None
    assert config.tier(TierName.FLASH).model == "gemini-2.5-flash"  # type: ignore[union-attr]
    assert config.tier_configured(TierName.DECISION) is True
    assert config.decision_preview == "jev"
    assert config.configured_tiers == (
        TierName.DECISION,
        TierName.REASONING,
        TierName.FLASH,
    )
    assert store.load() == config


def test_decision_preview_respects_fallback_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tiers = _tiers()
    store.save_models(
        tiers=tiers,
        agent_routing=dict(DEFAULT_AGENT_ROUTING),
        pipeline=PipelineConfig(decision_fallback_order=(DecisionFallback.HEURISTIC,)),
        decision_route=DecisionRoute.TYPESAFE_JEV,
        decision_policy=DecisionPolicyConfig(),
    )
    assert store.load().decision_preview == "heuristic"


def test_provider_keys_accept_typesafe_jev(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_api_key(Provider.TYPESAFE_JEV, "jev-synthetic-key-1234")
    config = store.load()
    assert any(
        key.provider is Provider.TYPESAFE_JEV and key.configured
        for key in config.api_keys
    )
    assert store.api_key(Provider.TYPESAFE_JEV) == "jev-synthetic-key-1234"
    store.clear_api_key(Provider.TYPESAFE_JEV)
    assert store.api_key(Provider.TYPESAFE_JEV) is None


def test_legacy_config_rejects_typesafe_jev(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SettingsError, match="Models"):
        store.save_config(
            provider=Provider.TYPESAFE_JEV,
            model_name="jev-1.14.0",
            news_feed_url="https://example.com/feed.xml",
            news_memory_ttl_days=30,
        )


def test_tier_validation_rejects_bad_combinations() -> None:
    with pytest.raises(SettingsError, match="decision tier"):
        validate_tier_config(
            TierName.DECISION,
            TierConfig(provider=Provider.GEMINI, model="x"),
        )
    with pytest.raises(SettingsError, match="only valid"):
        validate_tier_config(
            TierName.REASONING,
            TierConfig(provider=Provider.TYPESAFE_JEV, model="x"),
        )
    with pytest.raises(SettingsError, match="ladder"):
        validate_tier_config(
            TierName.REASONING,
            TierConfig(
                provider=Provider.GEMINI,
                model="x",
                fallback_tier=TierName.FLASH,
            ),
        )
    with pytest.raises(SettingsError, match="Temperature"):
        validate_tier_config(
            TierName.FLASH,
            TierConfig(provider=Provider.GEMINI, model="x", temperature=3.0),
        )


def test_fallback_order_and_routing_validation() -> None:
    with pytest.raises(SettingsError, match="repeat"):
        validate_fallback_order(["flash", "flash"])
    with pytest.raises(SettingsError, match="Unknown"):
        validate_fallback_order(["decision"])
    with pytest.raises(SettingsError, match="snake_case"):
        validate_agent_routing({"bad agent": AgentRoute(TierName.FLASH)})
    with pytest.raises(SettingsError, match="decision tier"):
        validate_agent_routing(
            {"agent": AgentRoute(TierName.FLASH, provider=Provider.TYPESAFE_JEV)}
        )


def test_save_models_rejects_unimplemented_route(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SettingsError, match="not implemented"):
        store.save_models(
            tiers=_tiers(),
            agent_routing=dict(DEFAULT_AGENT_ROUTING),
            pipeline=PipelineConfig(),
            decision_route=DecisionRoute.OPENROUTER_JEV,
            decision_policy=DecisionPolicyConfig(),
        )


def test_save_models_rejects_an_incomplete_tier_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SettingsError, match="Exactly"):
        store.save_models(
            tiers={TierName.FLASH: _tiers()[TierName.FLASH]},
            agent_routing=dict(DEFAULT_AGENT_ROUTING),
            pipeline=PipelineConfig(),
            decision_route=DecisionRoute.AUTO,
            decision_policy=DecisionPolicyConfig(),
        )
