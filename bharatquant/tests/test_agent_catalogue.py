from datetime import UTC, datetime

from bq.agent_catalogue import (
    AGENT_CATALOGUE,
    KIND_COORDINATOR,
    KIND_DAG_NODE,
    KIND_DECISION,
    KIND_DETERMINISTIC,
    KIND_LOOP,
    catalogue_for,
)
from bq.settings import (
    DEFAULT_AGENT_ROUTING,
    ApiKeyStatus,
    AppConfig,
    Provider,
    TierName,
)

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def _config() -> AppConfig:
    return AppConfig(
        provider=Provider.GEMINI,
        model_name="gemini-2.5-pro",
        news_feed_url="https://example.com/feed",
        news_memory_ttl_days=30,
        api_keys=(ApiKeyStatus(Provider.GEMINI, True, "********abcd"),),
        updated_at=NOW,
        agent_routing=DEFAULT_AGENT_ROUTING,
    )


def test_catalogue_covers_every_kind() -> None:
    ids = {entry.agent_id for entry in AGENT_CATALOGUE}
    assert "research" in ids
    assert "news_research" in ids
    assert "bull_debater" in ids
    assert "super_agent_cio" in ids
    assert "final_decision_gate" in ids
    assert {entry.kind for entry in AGENT_CATALOGUE} == {
        KIND_LOOP,
        KIND_DAG_NODE,
        KIND_DECISION,
        KIND_COORDINATOR,
        KIND_DETERMINISTIC,
    }


def test_research_entry_carries_tools_and_prompt() -> None:
    research = next(item for item in AGENT_CATALOGUE if item.agent_id == "research")
    assert research.kind == KIND_LOOP
    assert research.prompt_id == "research"
    assert research.read_only is False
    assert {"news_search", "stock_quote", "response"} <= set(research.tools)


def test_catalogue_for_annotates_resolved_routes() -> None:
    annotated = catalogue_for(_config())
    research = next(item for item in annotated if item.agent_id == "research")
    assert research.route_tier == TierName.FLASH.value
    assert research.provider is None
    assert research.model is None
    assert research.configured is False


def test_catalogue_to_dict_shape() -> None:
    entry = next(item for item in AGENT_CATALOGUE if item.agent_id == "research")
    payload = entry.to_dict()
    assert payload["agent_id"] == "research"
    assert payload["kind"] == KIND_LOOP
    assert payload["tools"] == list(entry.tools)
