from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from bq.decision.battery import SCREENER
from bq.decision.client import JevClient, JevUnavailable
from bq.decision.state import DecisionState
from bq.decision.types import Answer, JudgmentSet, QuestionType
from bq.infra.database import migrate
from bq.llm_client import ModelClient
from bq.llm_result import LLMResult
from bq.model_router import ModelRouter, RoutedModelCaller
from bq.settings import (
    DEFAULT_AGENT_ROUTING,
    DecisionFallback,
    DecisionPolicyConfig,
    DecisionRoute,
    PipelineConfig,
    Provider,
    SettingsStore,
    TierConfig,
    TierName,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
FLASH_JSON = (
    '{"setup_direction":"long","setup_quality":3,'
    '"worth_deep_research":0.8,"event_risk":0.1}'
)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


class FakeModelClient:
    def __init__(self, *, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0

    def complete(
        self,
        *,
        model: str,
        api_key: str,
        system: str,
        prompt: str,
        timeout: float = 60.0,
    ) -> LLMResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return LLMResult(text=self.text)


class FakeJev:
    def __init__(
        self,
        *,
        judgments: JudgmentSet | None = None,
        error: Exception | None = None,
    ) -> None:
        self.judgments = judgments
        self.error = error
        self.calls = 0

    def decide(self, **_kwargs: object) -> JudgmentSet:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.judgments is not None
        return self.judgments


def _tiers(
    *, decision_provider: Provider | None = Provider.TYPESAFE_JEV
) -> dict[TierName, TierConfig]:
    return {
        TierName.DECISION: TierConfig(
            provider=decision_provider,
            model="jev-1.14",
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


def _store(
    tmp_path: Path, *, decision_provider: Provider | None = Provider.TYPESAFE_JEV
) -> SettingsStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = SettingsStore(path, FrozenClock())
    store.save_models(
        tiers=_tiers(decision_provider=decision_provider),
        agent_routing=dict(DEFAULT_AGENT_ROUTING),
        pipeline=PipelineConfig(),
        decision_route=DecisionRoute.AUTO,
        decision_policy=DecisionPolicyConfig(),
    )
    return store


def _state() -> DecisionState:
    return DecisionState(
        as_of=NOW.isoformat(),
        symbol="NSE:RELIANCE",
        timeframe="swing",
        last_close=100.0,
        bar_count=120,
        composite=0.4,
        trend=0.5,
        momentum=0.3,
        volatility=-0.2,
        structure=0.4,
        rsi_14=55.0,
        atr_14=2.0,
        adx_14=27.0,
        volume_relative=1.2,
        bollinger_bandwidth=0.05,
        macd_histogram=0.2,
        support_distance_pct=-0.05,
        resistance_distance_pct=0.05,
        data_freshness="delayed_yfinance",
    )


def _judgments(*, model: str = "jev-1.14.0") -> JudgmentSet:
    return JudgmentSet(
        battery=SCREENER,
        answers=(
            (
                "setup_direction",
                Answer(kind=QuestionType.CHOICE, choice="long", confidence=0.9),
            ),
            (
                "setup_quality",
                Answer(kind=QuestionType.SCORE, score=3, confidence=0.8),
            ),
            ("worth_deep_research", Answer(kind=QuestionType.NOUL, noul=0.8)),
            ("event_risk", Answer(kind=QuestionType.NOUL, noul=0.1)),
        ),
        model=model,
        source="jev_native",
        latency_ms=12.0,
        request_id="req-1",
        model_mismatch=model != "jev-1.14",
    )


def test_jev_native_wins_when_configured(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_api_key(Provider.TYPESAFE_JEV, "jev-key-12345678")
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-key-1234")
    llm = FakeModelClient(text=FLASH_JSON)
    jev = FakeJev(judgments=_judgments())
    router = ModelRouter(
        store,
        clients={Provider.GEMINI: cast(ModelClient, llm)},
        jev=cast(JevClient, jev),
    )
    outcome = router.decide("watchlist_screener", state=_state(), battery=SCREENER)
    assert outcome.source == "jev_native"
    assert outcome.provider == "TYPESAFE_JEV"
    assert outcome.fallback_used is False
    assert jev.calls == 1
    assert llm.calls == 0
    assert router.routes_used[0].decision_source == "jev_native"


def test_no_jev_key_uses_flash_fallback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-key-1234")
    llm = FakeModelClient(text=FLASH_JSON)
    jev = FakeJev(judgments=_judgments())
    router = ModelRouter(
        store,
        clients={Provider.GEMINI: cast(ModelClient, llm)},
        jev=cast(JevClient, jev),
    )
    outcome = router.decide("watchlist_screener", state=_state(), battery=SCREENER)
    assert outcome.source == "llm_fallback"
    assert outcome.tier == "flash"
    assert outcome.provider == "GEMINI"
    assert outcome.model == "gemini-2.5-flash"
    assert outcome.fallback_used is True
    assert outcome.fallback_from == "unavailable"
    assert jev.calls == 0
    assert llm.calls == 1


def test_jev_failure_falls_back_and_records_the_reason(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_api_key(Provider.TYPESAFE_JEV, "jev-key-12345678")
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-key-1234")
    llm = FakeModelClient(text=FLASH_JSON)
    jev = FakeJev(error=JevUnavailable("down"))
    router = ModelRouter(
        store,
        clients={Provider.GEMINI: cast(ModelClient, llm)},
        jev=cast(JevClient, jev),
    )
    outcome = router.decide("watchlist_screener", state=_state(), battery=SCREENER)
    assert outcome.source == "llm_fallback"
    assert outcome.fallback_from == "JevUnavailable"
    assert jev.calls == 1
    assert llm.calls == 1


def test_no_keys_falls_back_to_the_deterministic_heuristic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    router = ModelRouter(store, clients={})
    outcome = router.decide("watchlist_screener", state=_state(), battery=SCREENER)
    assert outcome.source == "heuristic"
    assert outcome.provider == "code"
    assert outcome.model == "heuristic"
    assert outcome.fallback_used is True
    assert outcome.judgments.answer("setup_direction").choice == "long"
    assert outcome.judgments.answer("setup_direction").confidence == 0.5


def test_heuristic_can_be_the_whole_fallback_order(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = SettingsStore(path, FrozenClock())
    tiers = _tiers(decision_provider=None)
    store.save_models(
        tiers=tiers,
        agent_routing=dict(DEFAULT_AGENT_ROUTING),
        pipeline=PipelineConfig(decision_fallback_order=(DecisionFallback.HEURISTIC,)),
        decision_route=DecisionRoute.AUTO,
        decision_policy=DecisionPolicyConfig(),
    )
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-key-1234")
    llm = FakeModelClient(text=FLASH_JSON)
    router = ModelRouter(store, clients={Provider.GEMINI: cast(ModelClient, llm)})
    outcome = router.decide("watchlist_screener", state=_state(), battery=SCREENER)
    assert outcome.source == "heuristic"
    assert llm.calls == 0
    assert router.availability().preview == "heuristic"


def test_availability_preview_tracks_configuration(tmp_path: Path) -> None:
    store = _store(tmp_path)
    router = ModelRouter(store)
    assert router.availability().preview == "heuristic"
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-key-1234")
    assert router.availability().preview == "flash"
    store.set_api_key(Provider.TYPESAFE_JEV, "jev-key-12345678")
    assert router.availability().preview == "jev"


def test_text_tier_switches_from_configuration_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_api_key(Provider.GEMINI, "AIza-synthetic-key-1234")
    llm = FakeModelClient(text="ok")
    router = ModelRouter(store, clients={Provider.GEMINI: cast(ModelClient, llm)})
    caller = RoutedModelCaller(router)
    outcome = caller.complete(
        prompt="p",
        system="s",
        task_id="analysis.bull",
        task_version="v1",
    )
    assert outcome.provider is Provider.GEMINI
    assert outcome.model == "gemini-2.5-pro"
    route = caller.routes_used[0]
    assert route.agent_id == "bull_debater"
    assert route.tier == "reasoning"
    assert route.decision_source == "llm_direct"
