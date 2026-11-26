"""One place that assembles an :class:`AnalysisPipeline`.

The API and the campaign orchestrator must not drift in how they wire the debate,
the routing, or whether they attach a model at all. This module is the single
builder they share.
"""

from __future__ import annotations

from pathlib import Path

from bq.agent_prompts import PromptStore
from bq.analysis.confidence_engine import ConfidenceEngine
from bq.analysis.debate import Debate
from bq.analysis.evidence import EvidenceVerifier
from bq.analysis.final_gate import FinalGate
from bq.analysis.fundamental import FundamentalEventsAgent
from bq.analysis.news_context import NewsContextAgent
from bq.analysis.pipeline import AnalysisPipeline
from bq.analysis.regime import MarketRegimeAgent
from bq.analysis.risk_team import RiskAnalyst
from bq.analysis.wiring import build_analysis_router
from bq.data.vendors import VendorRouter
from bq.llm_service import LLMService
from bq.model_router import ModelRouter, RoutedModelCaller, decision_policy_for
from bq.news import NewsStore
from bq.settings import AppConfig, SettingsStore


def build_analysis_pipeline(
    database_path: Path, config: AppConfig, settings_store: SettingsStore
) -> AnalysisPipeline:
    """Build a ready-to-run pipeline for this configuration."""
    router = build_analysis_router(database_path, config)
    prompts = PromptStore(settings_store.path)
    news_store = NewsStore(settings_store.path)
    model_router = ModelRouter(settings_store, prompts=prompts)
    gate = FinalGate(model_router, decision_policy_for(config))
    if config.any_tier_configured:
        caller = RoutedModelCaller(model_router)
        return AnalysisPipeline(
            router,
            debate=Debate(caller, prompts=prompts),
            risk_analyst=RiskAnalyst(caller, prompts=prompts),
            routing=caller,
            news=NewsContextAgent(news_store),
            regime=MarketRegimeAgent(router),
            fundamental=FundamentalEventsAgent(news_store),
            final_gate=gate,
            evidence=EvidenceVerifier(news_store, router=model_router),
            confidence=ConfidenceEngine(settings_store.path),
        )
    service = LLMService(settings_store) if config.model_route_configured else None
    return AnalysisPipeline(
        router,
        debate=Debate(service, prompts=prompts) if service is not None else None,
        risk_analyst=(
            RiskAnalyst(service, prompts=prompts) if service is not None else None
        ),
        news=NewsContextAgent(news_store),
        regime=MarketRegimeAgent(router),
        fundamental=FundamentalEventsAgent(news_store),
        final_gate=gate,
        evidence=EvidenceVerifier(news_store, router=model_router),
        confidence=ConfidenceEngine(settings_store.path),
    )


def build_deterministic_pipeline(
    router: VendorRouter, *, clock: object | None = None
) -> AnalysisPipeline:
    """The cheapest correct pipeline: indicators, rules, plan. No model call."""
    return AnalysisPipeline(router, clock=clock)  # type: ignore[arg-type]


__all__ = ["build_analysis_pipeline", "build_deterministic_pipeline"]
