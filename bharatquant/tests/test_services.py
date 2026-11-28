from pathlib import Path

from bq.analysis.pipeline import AnalysisPipeline
from bq.campaign import CampaignService
from bq.infra.database import migrate
from bq.model_router import ModelRouter
from bq.news_agent import NewsResearchAgent
from bq.research_agent import ResearchAgent
from bq.services import build_services
from bq.settings import SettingsStore


def test_build_services_wires_shared_objects(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    settings = SettingsStore(path)
    services = build_services(path, settings)

    assert isinstance(services.model_router, ModelRouter)
    assert isinstance(services.pipeline(), AnalysisPipeline)
    assert isinstance(services.research_agent(), ResearchAgent)
    assert isinstance(services.news_agent(), NewsResearchAgent)
    assert isinstance(services.campaign_service(), CampaignService)
