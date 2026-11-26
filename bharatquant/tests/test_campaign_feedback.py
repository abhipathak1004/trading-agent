from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

from bq.campaign import CampaignStore
from bq.campaign_feedback import (
    campaign_review,
    event_statistics,
    event_weights,
    resolve_outcomes,
)
from bq.core import EXCHANGE_TIMEZONE
from bq.data.vendors.base import Bar
from bq.data.vendors.router import VendorResult
from bq.infra.database import migrate

NOW = datetime(2026, 11, 26, 11, 30, tzinfo=UTC)  # 17:00 IST
TRADE_DATE = date(2026, 11, 20)


class FakeClock:
    def now(self) -> datetime:
        return NOW


class FakeRouter:
    def __init__(self, bars: tuple[Bar, ...]) -> None:
        self._bars = bars

    def bars(self, symbol, *, interval, start, end, domain=None):
        data = tuple(bar for bar in self._bars if start <= bar.event_at <= end)
        return VendorResult(data=data, source="fake", is_realtime=False)


def _events(*names: str) -> list[dict[str, object]]:
    return [
        {"event": name, "polarity": 1, "strength": 4, "news_id": "n1"} for name in names
    ]


def _setup(tmp_path: Path) -> tuple[Path, CampaignStore]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = CampaignStore(path, FakeClock())
    store.upsert_campaign(
        campaign_id="camp_20261120",
        trade_date=TRADE_DATE,
        as_of=datetime(2026, 11, 20, 3, 45, tzinfo=UTC),
        config={"account_id": "paper-main"},
        status="ACTIVE",
        summary={},
    )
    return path, store


def test_event_statistics_and_weights(tmp_path: Path) -> None:
    path, store = _setup(tmp_path)
    store.upsert_item(
        campaign_id="camp_20261120",
        symbol="A",
        rank=1,
        catalyst_score=5.0,
        events=_events("ORDER_WIN"),
        verdict="CANDIDATE",
        status="CLOSED",
    )
    store.update_item("camp_20261120", "A", r_multiple=1.5)
    store.upsert_item(
        campaign_id="camp_20261120",
        symbol="B",
        rank=2,
        catalyst_score=4.0,
        events=_events("ORDER_WIN"),
        verdict="CANDIDATE",
        status="CLOSED",
    )
    store.update_item("camp_20261120", "B", r_multiple=1.0)
    store.upsert_item(
        campaign_id="camp_20261120",
        symbol="C",
        rank=3,
        catalyst_score=3.0,
        events=_events("EARNINGS_STRONG"),
        verdict="CANDIDATE",
        status="CLOSED",
    )
    store.update_item("camp_20261120", "C", r_multiple=-1.0)

    stats = {stat.event: stat for stat in event_statistics(path)}
    assert stats["ORDER_WIN"].closed == 2
    assert stats["ORDER_WIN"].wins == 2
    assert stats["ORDER_WIN"].hit_rate == 1.0
    assert stats["ORDER_WIN"].avg_r == 1.25
    assert stats["EARNINGS_STRONG"].wins == 0

    weights = event_weights(path)
    assert weights["ORDER_WIN"] > 1.0
    assert weights["EARNINGS_STRONG"] < 1.0
    assert 0.5 <= weights["ORDER_WIN"] <= 1.5


def test_empty_history_keeps_neutral_weights(tmp_path: Path) -> None:
    path, _store = _setup(tmp_path)
    assert event_statistics(path) == ()
    assert event_weights(path) == {}


def test_review_includes_realised_pnl_and_open_positions(tmp_path: Path) -> None:
    path, store = _setup(tmp_path)
    store.upsert_item(
        campaign_id="camp_20261120",
        symbol="A",
        rank=1,
        catalyst_score=5.0,
        events=_events("ORDER_WIN"),
        verdict="CANDIDATE",
        status="CLOSED",
    )
    store.update_item(
        "camp_20261120",
        "A",
        outcome={
            "state": "CLOSED",
            "entry_paise": 100_000,
            "exit_paise": 110_000,
            "net_pnl_paise": 9_000,
        },
        r_multiple=1.0,
    )
    review = campaign_review(path, trade_date=TRADE_DATE)
    assert review["campaign"]["campaign_id"] == "camp_20261120"
    assert review["realized_pnl_paise"] == 9_000
    assert review["items"][0]["symbol"] == "A"
    assert review["open_positions"] == []
    assert len(review["event_stats"]) == 1


def test_reconciliation_uses_first_touch_when_position_is_flat(tmp_path: Path) -> None:
    path, store = _setup(tmp_path)
    filled_at = datetime(2026, 11, 20, 4, 0, tzinfo=UTC)
    store.upsert_item(
        campaign_id="camp_20261120",
        symbol="RELIANCE",
        rank=1,
        catalyst_score=5.0,
        events=_events("ORDER_WIN"),
        verdict="CANDIDATE",
        status="FILLED",
        plan={"symbol": "NSE:RELIANCE", "product": "CNC"},
    )
    store.update_item(
        "camp_20261120",
        "RELIANCE",
        outcome={
            "state": "FILLED",
            "entry_paise": 100_000,
            "stop_paise": 90_000,
            "target_paise": 120_000,
            "filled_at": filled_at.isoformat(),
        },
    )
    bars = (
        Bar(
            event_at=datetime.combine(
                date(2026, 11, 23), time(15, 25), tzinfo=EXCHANGE_TIMEZONE
            ).astimezone(UTC),
            open=Decimal("1000"),
            high=Decimal("1010"),
            low=Decimal("890"),
            close=Decimal("900"),
            volume=100_000,
        ),
    )
    result = resolve_outcomes(path, FakeRouter(bars), as_of=NOW, horizon_days=5)
    assert result.reconciled == 1
    item = store.get("camp_20261120").items[0]
    assert item.status == "CLOSED"
    assert item.r_multiple == -1.0
    assert item.outcome is not None
    assert item.outcome["exit_reason"] == "stop"
