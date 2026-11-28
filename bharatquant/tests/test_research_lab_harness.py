"""Prompt-prefix stability, compaction, and the paper-only mandate gate."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.research_lab.mandate import (
    REASON_ALLOWED,
    REASON_EXPIRED,
    REASON_EXPOSURE,
    REASON_KILL_SWITCH,
    REASON_NOTIONAL,
    REASON_REVOKED,
    REASON_SYMBOL,
    MandateError,
    MandateStore,
    OrderIntent,
    append_audit_event,
    gate_order_intent,
    kill_switch_active,
    submit_with_mandate,
    verify_audit_chain,
)
from bq.research_lab.prompting import (
    ResearchPromptBuilder,
    compact_observations,
)

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


# --- prompt prefix -------------------------------------------------------


def _builder() -> ResearchPromptBuilder:
    return ResearchPromptBuilder(
        instructions="You are a research planner.",
        tools_prompt="## available tools\n- stock_quote",
        skill_rules="Always record as_of.",
    )


def test_static_prefix_is_byte_identical_across_instances() -> None:
    assert _builder().static_prefix() == _builder().static_prefix()


def test_dynamic_context_never_changes_the_prefix() -> None:
    builder = _builder()
    prefix = builder.static_prefix()
    first = builder.build(as_of="2026-11-28T00:00:00Z", run_ids=["run_a"])
    second = builder.build(as_of="2026-11-29T00:00:00Z", run_ids=["run_b", "run_c"])
    assert first.system == second.system == prefix
    assert first.prompt != second.prompt
    assert "as_of: 2026-11-28T00:00:00Z" in first.prompt
    assert "active runs: run_b, run_c" in second.prompt


def test_empty_blocks_are_refused() -> None:
    with pytest.raises(ValueError):
        ResearchPromptBuilder(instructions=" ", tools_prompt="x")
    with pytest.raises(ValueError):
        _builder().build(as_of="")


def test_compaction_keeps_the_newest_and_their_refs() -> None:
    observations = [
        {"run_id": f"run_{index}", "text": "x" * 400, "artifact_refs": [f"art_{index}"]}
        for index in range(10)
    ]
    kept, compacted, omitted = compact_observations(
        observations, threshold_tokens=1000, count_tokens=lambda text: len(text)
    )
    assert compacted is True
    assert omitted == len(observations) - (len(kept) - 1)
    assert kept[0]["kind"] == "compaction_summary"
    assert "run_0" in str(kept[0]["artifact_refs"])
    assert kept[-1]["run_id"] == "run_9"


def test_no_compaction_below_the_threshold() -> None:
    observations = [{"run_id": "run_a", "text": "short"}]
    kept, compacted, omitted = compact_observations(
        observations, threshold_tokens=1000, count_tokens=lambda text: len(text)
    )
    assert (kept, compacted, omitted) == (observations, False, 0)


# --- mandate gate --------------------------------------------------------


def _store(tmp_path: Path) -> MandateStore:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return MandateStore(path)


def _intent(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 10,
        "price_paise": 250_000,
    }
    values.update(overrides)
    return OrderIntent(**values)  # type: ignore[arg-type]


def test_a_valid_intent_within_mandate_is_allowed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mandate = store.create(
        account_id="paper-main",
        allowed_symbols=["RELIANCE"],
        max_order_notional_paise=10_000_000,
        max_daily_exposure_paise=50_000_000,
        expires_at=NOW + timedelta(days=1),
    )
    decision = gate_order_intent(
        intent=_intent(),
        mandate=mandate,
        daily_exposure_paise=0,
        now=NOW,
        kill_switch=False,
    )
    assert decision.allowed is True
    assert decision.reason == REASON_ALLOWED


def test_every_boundary_blocks_with_its_own_reason(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tight = store.create(
        account_id="paper-tight",
        allowed_symbols=["RELIANCE"],
        max_order_notional_paise=1_000,
        max_daily_exposure_paise=1_500,
        expires_at=NOW + timedelta(days=1),
    )
    permissive = store.create(
        account_id="paper-wide",
        allowed_symbols=["RELIANCE"],
        max_order_notional_paise=10_000_000,
        max_daily_exposure_paise=2_000_000,
        expires_at=NOW + timedelta(days=1),
    )
    revoked = store.revoke(permissive.mandate_id)
    cases = [
        (True, permissive, 0, REASON_KILL_SWITCH),
        (False, None, 0, "no_mandate"),
        (False, revoked, 0, REASON_REVOKED),
        (False, tight, 0, REASON_NOTIONAL),
        (False, permissive, 900_000, REASON_EXPOSURE),
    ]
    for kill_switch, active, exposure, expected in cases:
        decision = gate_order_intent(
            intent=_intent(),
            mandate=active,
            daily_exposure_paise=exposure,
            now=NOW,
            kill_switch=kill_switch,
        )
        assert decision.allowed is False, expected
        assert decision.reason == expected


def test_expired_and_out_of_mandate_symbols_block(tmp_path: Path) -> None:
    store = _store(tmp_path)
    expired = store.create(
        account_id="paper-main",
        allowed_symbols=["RELIANCE"],
        max_order_notional_paise=10_000_000,
        max_daily_exposure_paise=10_000_000,
        expires_at=NOW - timedelta(seconds=1),
    )
    decision = gate_order_intent(
        intent=_intent(),
        mandate=expired,
        daily_exposure_paise=0,
        now=NOW,
        kill_switch=False,
    )
    assert decision.reason == REASON_EXPIRED

    narrow = store.create(
        account_id="paper-two",
        allowed_symbols=["INFY"],
        max_order_notional_paise=10_000_000,
        max_daily_exposure_paise=10_000_000,
        expires_at=NOW + timedelta(days=1),
    )
    decision = gate_order_intent(
        intent=_intent(),
        mandate=narrow,
        daily_exposure_paise=0,
        now=NOW,
        kill_switch=False,
    )
    assert decision.reason == REASON_SYMBOL


def test_a_blocked_intent_never_invokes_the_executor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mandate = store.create(
        account_id="paper-main",
        allowed_symbols=["RELIANCE"],
        max_order_notional_paise=10_000_000,
        max_daily_exposure_paise=10_000_000,
        expires_at=NOW + timedelta(days=1),
    )
    calls: list[OrderIntent] = []

    def execute(intent: OrderIntent) -> str:
        calls.append(intent)
        return "submitted"

    blocked, result = submit_with_mandate(
        _intent(),
        mandate=mandate,
        daily_exposure_paise=0,
        now=NOW,
        kill_switch=True,
        execute=execute,
    )
    assert blocked.reason == REASON_KILL_SWITCH
    assert result is None
    assert calls == []

    allowed, result = submit_with_mandate(
        _intent(),
        mandate=mandate,
        daily_exposure_paise=0,
        now=NOW,
        kill_switch=False,
        execute=execute,
    )
    assert allowed.allowed is True
    assert result == "submitted"
    assert len(calls) == 1


def test_live_broker_mandates_are_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(MandateError, match="ADR-001"):
        store.create(
            account_id="paper-main",
            allowed_symbols=["RELIANCE"],
            max_order_notional_paise=1,
            max_daily_exposure_paise=1,
            expires_at=NOW + timedelta(days=1),
            broker="KITE",
        )


def test_kill_switch_is_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("BQ_KILL_SWITCH_PATH", raising=False)
    assert kill_switch_active(home) is False
    switch = home / "data" / "KILL_SWITCH"
    switch.parent.mkdir(parents=True)
    switch.write_text("halt", encoding="utf-8")
    assert kill_switch_active(home) is True


def test_audit_events_feed_the_existing_chain(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    append_audit_event(
        path,
        event_type="research.run.blocked",
        entity_id="run_1",
        payload={"reason": "x"},
    )
    assert verify_audit_chain(path) is True

    # A hand-written row with a bogus hash breaks the chain; the triggers stop
    # update/delete, so the only way to simulate tampering is a forged insert.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO audit_events (sequence, event_id, occurred_at, actor, "
            "action, object_type, object_id, config_version, payload_hash, "
            "previous_hash, event_hash) VALUES "
            "((SELECT MAX(sequence)+1 FROM audit_events), 'forged', "
            "'2026-11-28T00:00:00.000000Z', 'attacker', 'x', 'y', 'z', 1, "
            "'" + "0" * 64 + "', '" + "0" * 64 + "', '" + "f" * 64 + "')"
        )
    assert verify_audit_chain(path) is False
