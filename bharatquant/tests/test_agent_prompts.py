import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from bq.agent_prompts import (
    DEFINITIONS,
    PromptError,
    PromptNotFound,
    PromptStore,
)
from bq.analysis.debate import Debate
from bq.analysis.prompts import BULL_TEMPLATE
from bq.analysis.technical import SubScore, TechnicalReport
from bq.compute.indicators import IndicatorSnapshot
from bq.infra.database import migrate

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _store(tmp_path: Path) -> PromptStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return PromptStore(path, FrozenClock())


def _report() -> TechnicalReport:
    indicator = IndicatorSnapshot(
        as_of=NOW,
        bar_count=120,
        close=100.0,
        sma_20=98.0,
        sma_50=95.0,
        ema_20=99.0,
        rsi_14=58.0,
        macd=None,
        bollinger=None,
        atr_14=2.5,
        vwap=97.5,
        adx_14=27.0,
        max_drawdown=0.12,
        volume=None,
        swings=None,
    )
    return TechnicalReport(
        symbol="NSE:RELIANCE",
        as_of=NOW,
        timeframe="swing",
        last_close=Decimal("100.00"),
        bar_count=120,
        trend=SubScore(0.6, ("close above sma_50",)),
        momentum=SubScore(0.4, ("rsi above 50",)),
        volatility=SubScore(-0.2, ("atr elevated",)),
        structure=SubScore(0.5, ("support below",)),
        indicators=indicator,
    )


class FakeCaller:
    def __init__(self, text: str) -> None:
        self.text = text
        self.system = ""

    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        task_id: str = "",
        task_version: str = "",
    ) -> object:
        self.system = system
        from bq.llm_result import LLMResult

        return LLMResult(text=self.text)


def test_defaults_cover_the_analysis_agents(tmp_path: Path) -> None:
    prompts = _store(tmp_path).list()
    assert {prompt.agent_id for prompt in prompts} == {
        definition.agent_id for definition in DEFINITIONS
    }
    bull = next(prompt for prompt in prompts if prompt.agent_id == "bull_debater")
    assert bull.is_default is True
    assert bull.version == 0
    assert bull.updated_at is None
    assert "facts" in bull.required_fields


def test_save_appends_versions_and_reset_returns_to_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.save("bull_debater", system="CUSTOM BULL", template=BULL_TEMPLATE)
    assert first.version == 1
    assert first.system == "CUSTOM BULL"
    assert first.is_default is False
    assert first.updated_at == NOW

    second = store.save("bull_debater", system="CUSTOM BULL 2", template=BULL_TEMPLATE)
    assert second.version == 2
    assert store.get("bull_debater").system == "CUSTOM BULL 2"

    reset = store.reset("bull_debater")
    assert reset.version == 3
    assert reset.is_default is True
    assert reset.system != "CUSTOM BULL 2"


def test_prompt_validation_rejects_broken_templates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(PromptNotFound, match="Unknown agent"):
        store.save("nope", system="x", template="")
    with pytest.raises(PromptError, match="empty"):
        store.save("bull_debater", system="", template=BULL_TEMPLATE)
    with pytest.raises(PromptError, match="missing"):
        store.save("bull_debater", system="x", template="Symbol: {symbol}")
    with pytest.raises(PromptError, match="unknown field"):
        store.save(
            "bull_debater",
            system="x",
            template=BULL_TEMPLATE + "\n{unexpected}",
        )
    with pytest.raises(PromptError, match="simple placeholder"):
        store.save("bull_debater", system="x", template=BULL_TEMPLATE + "\n{}")
    with pytest.raises(PromptError, match="braces"):
        store.save("bull_debater", system="x", template=BULL_TEMPLATE + "\n{")


def test_research_and_news_prompts_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    research = store.get("research")
    assert research.required_fields == ("task", "history")
    assert "task" in research.template and "history" in research.template
    news = store.get("news_research")
    assert news.template == ""
    assert news.required_fields == ()

    saved = store.save("research", system="CUSTOM", template="{task}\n\n{history}")
    assert saved.version == 1
    assert saved.system == "CUSTOM"
    reset = store.reset("research")
    assert reset.is_default is True
    assert reset.system != "CUSTOM"


def test_prompt_saves_are_audited_without_the_prompt_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save("bull_debater", system="SECRET PROMPT TEXT", template=BULL_TEMPLATE)
    with closing(sqlite3.connect(tmp_path / "bq.db")) as connection:
        rows = connection.execute(
            "SELECT action, object_id, payload_hash FROM audit_events ORDER BY sequence"
        ).fetchall()
    assert any(row[0] == "agent.prompt.saved" for row in rows)
    assert all(len(row[2]) == 64 for row in rows)
    assert all("SECRET" not in str(row) for row in rows)


def test_debate_uses_an_overridden_bull_prompt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save("bull_debater", system="CUSTOM BULL SYSTEM", template=BULL_TEMPLATE)
    caller = FakeCaller('{"argument":"a","strongest_points":[]}')
    argument = Debate(caller, prompts=store).bull(_report())
    assert argument.argument == "a"
    assert caller.system == "CUSTOM BULL SYSTEM"
