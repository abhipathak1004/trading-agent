"""The Research Lab config block: round-trip, isolation and bounds."""

from decimal import Decimal
from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.settings import (
    DEFAULT_RESEARCH_LAB,
    EvidenceAuditorMode,
    ResearchBudgets,
    ResearchLabConfig,
    ResearchModelSlot,
    ResearchTriageConfig,
    SettingsError,
    SettingsStore,
    TierName,
    resolve_research_lab_models,
    validate_research_lab_config,
)


def _store(tmp_path: Path) -> SettingsStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return SettingsStore(path)


def test_defaults_are_the_documented_slots() -> None:
    lab = DEFAULT_RESEARCH_LAB
    assert lab.planner.tier is TierName.REASONING
    assert lab.synthesizer.tier is TierName.REASONING
    assert lab.evidence_auditor.tier is TierName.FLASH
    assert lab.evidence_auditor_mode is EvidenceAuditorMode.DETERMINISTIC
    assert lab.triage_scorer.tier is TierName.DECISION
    assert lab.budgets.max_iterations == 12
    assert lab.budgets.compaction_token_threshold == 24000


def test_round_trip_keeps_decimals_exact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    changed = ResearchLabConfig(
        planner=ResearchModelSlot(TierName.FLASH, 4096, 0.3),
        budgets=ResearchBudgets(
            max_iterations=7,
            max_recovery_retries=1,
            max_revision_retries=3,
            compaction_token_threshold=12000,
        ),
        evidence_auditor_mode=EvidenceAuditorMode.LLM,
    )
    store.save_research_lab(changed)
    again = store.load().research_lab
    assert again.planner == ResearchModelSlot(TierName.FLASH, 4096, 0.3)
    assert again.budgets == changed.budgets
    assert again.evidence_auditor_mode is EvidenceAuditorMode.LLM
    # A rate must survive the string round-trip without float drift.
    assert again.costs.exchange_rate == Decimal("0.0000297")
    assert again.costs.brokerage_bps == Decimal("0")


def test_session_override_is_memory_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_research_lab(ResearchLabConfig())
    before = store.load()
    resolved = resolve_research_lab_models(
        before, {"planner": {"tier": "flash", "model": "vendor/model-a"}}
    )
    after = store.load()
    assert resolved["research_lab_planner"].tier is TierName.FLASH
    assert resolved["research_lab_planner"].model == "vendor/model-a"
    # The stored configuration is untouched.
    assert after.research_lab.planner.tier is TierName.REASONING
    assert after.route_for("research_lab_planner").model is None


def test_override_accepts_slot_and_agent_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = store.load()
    by_slot = resolve_research_lab_models(config, {"synthesizer": {"tier": "flash"}})
    by_agent = resolve_research_lab_models(
        config, {"research_lab_synthesizer": {"tier": "flash"}}
    )
    assert by_slot == by_agent


def test_triage_cannot_leave_the_decision_tier(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SettingsError):
        resolve_research_lab_models(store.load(), {"triage_scorer": {"tier": "flash"}})
    with pytest.raises(SettingsError):
        validate_research_lab_config(
            ResearchLabConfig(
                triage_scorer=ResearchTriageConfig(enabled=True, tier=TierName.FLASH)
            )
        )


def test_text_slots_reject_the_decision_tier() -> None:
    with pytest.raises(SettingsError):
        validate_research_lab_config(
            ResearchLabConfig(planner=ResearchModelSlot(TierName.DECISION, 1024, 0.1))
        )


@pytest.mark.parametrize(
    "budgets",
    [
        ResearchBudgets(max_iterations=0),
        ResearchBudgets(max_iterations=1000),
        ResearchBudgets(max_recovery_retries=99),
        ResearchBudgets(compaction_token_threshold=10),
    ],
)
def test_budget_bounds_are_enforced(budgets: ResearchBudgets) -> None:
    with pytest.raises(SettingsError):
        validate_research_lab_config(ResearchLabConfig(budgets=budgets))


def test_unknown_override_slot_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SettingsError):
        resolve_research_lab_models(store.load(), {"wizard": {"tier": "flash"}})


def test_corrupt_row_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = SettingsStore(path)
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO app_settings (setting_key, setting_value, updated_at) "
            "VALUES ('research_lab', 'not json', '2026-01-01T00:00:00Z')"
        )
    assert store.load().research_lab == DEFAULT_RESEARCH_LAB
