"""One place that builds the services the API, CLI and scheduler share.

The API, CLI and job handlers used to re-instantiate the same objects (vendor
router, analysis pipeline, model router, research agent, campaign, trigger,
reports) independently, so they could drift. ``build_services`` keeps that wiring
in one place while still returning fresh objects where a caller needs one per
run (pipelines and routers read the current settings, not a cached snapshot).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bq.agent_prompts import PromptStore
from bq.agent_run_store import AgentRunStore
from bq.analysis.builder import build_analysis_pipeline
from bq.analysis.pipeline import AnalysisPipeline
from bq.analysis.wiring import build_analysis_router
from bq.campaign import CampaignService
from bq.model_router import ModelRouter
from bq.news_agent import NewsResearchAgent
from bq.reports.engine import ReportEngine
from bq.research_agent import ResearchAgent
from bq.settings import SettingsStore
from bq.triggers import TriggerEngine


@dataclass(frozen=True, slots=True)
class AppServices:
    database_path: Path
    settings: SettingsStore
    prompts: PromptStore
    model_router: ModelRouter
    agent_runs: AgentRunStore

    def pipeline(self) -> AnalysisPipeline:
        return build_analysis_pipeline(
            self.database_path, self.settings.load(), self.settings
        )

    def research_agent(self) -> ResearchAgent:
        config = self.settings.load()
        return ResearchAgent(
            self.database_path,
            feed_url=config.news_feed_url,
            router=self.model_router,
            prompts=self.prompts,
            run_store=self.agent_runs,
        )

    def news_agent(self) -> NewsResearchAgent:
        config = self.settings.load()
        return NewsResearchAgent(
            self.database_path,
            feed_url=config.news_feed_url,
        )

    def campaign_service(self) -> CampaignService:
        return CampaignService(self.database_path, self.settings)

    def trigger_engine(self) -> TriggerEngine:
        return TriggerEngine(self.database_path, self.settings)

    def report_engine(self) -> ReportEngine:
        router = build_analysis_router(self.database_path, self.settings.load())
        return ReportEngine(self.database_path, router)


def build_services(database_path: Path, settings: SettingsStore) -> AppServices:
    prompts = PromptStore(database_path)
    return AppServices(
        database_path=database_path,
        settings=settings,
        prompts=prompts,
        model_router=ModelRouter(settings, prompts=prompts),
        agent_runs=AgentRunStore(database_path),
    )


__all__ = [
    "AppServices",
    "build_services",
]
