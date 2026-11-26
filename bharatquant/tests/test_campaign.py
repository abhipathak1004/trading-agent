from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bq.campaign import (
    CampaignError,
    CampaignService,
    CampaignStore,
    campaign_trade_date,
)
from bq.campaign_config import CampaignConfig, CampaignConfigError
from bq.core import EXCHANGE_TIMEZONE
from bq.data.equity_master import EquityMasterStore
from bq.data.vendors.base import Bar, BarInterval
from bq.data.vendors.router import VendorResult
from bq.infra.database import migrate
from bq.news import FeedEntry, NewsStore
from bq.settings import SettingsStore

AS_OF = datetime(2026, 11, 26, 3, 45, tzinfo=UTC)  # 09:15 IST
TRADE_DATE = date(2026, 11, 26)

LISTING = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, "
    "MARKET LOT, ISIN NUMBER, FACE VALUE\n"
    "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
    "SUNPHARMA,Sun Pharmaceutical Industries Limited,EQ,08-JUL-1994,1,1,"
    "INE044A01036,1\n"
    "BEARISH,Bearish Industries Limited,EQ,08-JUL-1994,1,1,INE000000001,1\n"
)


class FrozenClock:
    def now(self) -> datetime:
        return AS_OF


class FakeRouter:
    def __init__(self, bars: dict[str, tuple[Bar, ...]]) -> None:
        self._bars = bars

    def bars(
        self,
        symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
        domain=None,
    ):
        data = tuple(
            bar
            for bar in self._bars.get(symbol.canonical, ())
            if start <= bar.event_at <= end
        )
        return VendorResult(data=data, source="fake", is_realtime=False)


def _bars(
    symbol: str,
    *,
    start_price: Decimal,
    step: Decimal,
    days: int = 90,
    end: date = date(2026, 11, 25),
) -> tuple[Bar, ...]:
    bars: list[Bar] = []
    price = start_price
    cursor = end - timedelta(days=days * 2)
    while cursor <= end:
        if cursor.weekday() < 5:
            close = price + step
            bars.append(
                Bar(
                    event_at=datetime.combine(
                        cursor, time(15, 25), tzinfo=EXCHANGE_TIMEZONE
                    ).astimezone(UTC),
                    open=price,
                    high=max(price, close) + Decimal("3"),
                    low=min(price, close) - Decimal("3"),
                    close=close,
                    volume=1_000_000,
                )
            )
            price = close
        cursor += timedelta(days=1)
    return tuple(bars)


def _news(path: Path) -> None:
    NewsStore(path, FrozenClock()).ingest(
        [
            FeedEntry(
                title="Reliance wins ₹2,000 crore order from ONGC",
                summary="",
                url="https://example.com/one",
                published_at=AS_OF - timedelta(hours=2),
            ),
            FeedEntry(
                title="Sun Pharmaceutical Q2 profit rises 30%",
                summary="",
                url="https://example.com/two",
                published_at=AS_OF - timedelta(hours=2),
            ),
            FeedEntry(
                title="Bearish Industries wins order worth ₹300 crore",
                summary="",
                url="https://example.com/three",
                published_at=AS_OF - timedelta(hours=2),
            ),
        ]
    )


def _service(tmp_path: Path) -> tuple[CampaignService, Path]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    _news(path)
    router = FakeRouter(
        {
            "NSE:RELIANCE": _bars(
                "RELIANCE", start_price=Decimal("1000"), step=Decimal("5")
            ),
            "NSE:SUNPHARMA": _bars(
                "SUNPHARMA", start_price=Decimal("800"), step=Decimal("4")
            ),
            "NSE:BEARISH": _bars(
                "BEARISH", start_price=Decimal("900"), step=Decimal("-5")
            ),
        }
    )
    service = CampaignService(
        path,
        SettingsStore(path),
        clock=FrozenClock(),
        router=router,
        news_fetcher=lambda _url: b"<rss><channel></channel></rss>",
        listing_fetcher=lambda: LISTING.encode("utf-8"),
    )
    return service, path


def test_trade_date_targets_next_session_after_close() -> None:
    assert campaign_trade_date(AS_OF) == TRADE_DATE
    after_close = datetime(2026, 11, 26, 11, 0, tzinfo=UTC)  # 16:30 IST
    assert campaign_trade_date(after_close) == date(2026, 11, 27)
    saturday = datetime(2026, 11, 28, 5, 0, tzinfo=UTC)
    assert campaign_trade_date(saturday) == date(2026, 11, 30)


def test_config_bounds_are_enforced() -> None:
    with pytest.raises(CampaignConfigError, match="top_n"):
        CampaignConfig(top_n=20)
    with pytest.raises(CampaignConfigError, match="available_cash_paise"):
        CampaignConfig(equity_paise=100, available_cash_paise=200)
    with pytest.raises(CampaignConfigError, match="valid_until"):
        CampaignConfig(valid_until="16:30")
    with pytest.raises(CampaignConfigError, match="Unknown"):
        CampaignConfig.from_mapping({"nope": 1})
    assert CampaignConfig.from_mapping(
        {"top_n": 6, "skip_dates": ["2026-01-26"]}
    ).skip_dates == ("2026-01-26",)
    assert len(CampaignConfig().feed_urls) == 3
    with pytest.raises(CampaignConfigError, match="HTTPS"):
        CampaignConfig(feed_urls=("http://insecure.example/feed",))


def test_research_builds_an_actionable_campaign(tmp_path: Path) -> None:
    service, path = _service(tmp_path)
    campaign = service.research(as_of=AS_OF)

    assert campaign.campaign_id == "camp_20261126"
    assert campaign.status == "ACTIVE"
    assert {item.symbol for item in campaign.items} == {
        "RELIANCE",
        "SUNPHARMA",
        "BEARISH",
    }

    reliance = next(item for item in campaign.items if item.symbol == "RELIANCE")
    assert reliance.status == "WATCHING"
    assert reliance.verdict == "CANDIDATE"
    assert reliance.analysis_run_id is not None
    assert reliance.plan is not None
    assert reliance.trigger is not None
    assert reliance.trigger["mode"] == "breakout"
    assert reliance.trigger["level_paise"] == reliance.trigger["previous_high_paise"]
    assert reliance.trigger["stop_paise"] < reliance.trigger["level_paise"]
    assert reliance.trigger["target_paise"] > reliance.trigger["level_paise"]
    assert reliance.trigger["valid_until"].startswith("2026-11-26T14:30")

    bearish = next(item for item in campaign.items if item.symbol == "BEARISH")
    assert bearish.status == "BLOCKED"
    assert bearish.verdict == "REJECTED"
    assert bearish.trigger is None

    assert EquityMasterStore(path, FrozenClock()).count() == 3
    assert campaign.summary["catalysts_stored"] == 3
    assert campaign.summary["candidates"] == 2


def test_research_is_idempotent_per_day(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    first = service.research(as_of=AS_OF)
    second = service.research(as_of=AS_OF)
    assert first.campaign_id == second.campaign_id
    assert first.created_at == second.created_at
    assert len(second.items) == len(first.items)


def test_force_is_refused_once_an_item_progressed(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    campaign = service.research(as_of=AS_OF)
    store = CampaignStore(service.path, FrozenClock())
    store.update_item(campaign.campaign_id, "RELIANCE", status="FILLED")
    with pytest.raises(CampaignError, match="progressed"):
        service.research(as_of=AS_OF, force=True)


def test_cancel_marks_watching_items(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    campaign = service.research(as_of=AS_OF)
    cancelled = service.cancel(campaign.campaign_id, as_of=AS_OF)
    assert cancelled.status == "CANCELLED"
    assert all(
        item.status == "CANCELLED"
        for item in cancelled.items
        if item.verdict == "CANDIDATE"
    )


def test_news_is_ingested_when_stale(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    router = FakeRouter(
        {
            "NSE:RELIANCE": _bars(
                "RELIANCE", start_price=Decimal("1000"), step=Decimal("5")
            )
        }
    )
    rss = (
        b"<?xml version='1.0'?><rss><channel><item>"
        b"<title>Reliance wins order worth Rs 500 crore</title>"
        b"<link>https://example.com/story</link>"
        b"<description>Large order</description>"
        b"<pubDate>Thu, 26 Nov 2026 02:00:00 +0000</pubDate>"
        b"</item></channel></rss>"
    )
    service = CampaignService(
        path,
        SettingsStore(path),
        clock=FrozenClock(),
        router=router,
        news_fetcher=lambda _url: rss,
        listing_fetcher=lambda: LISTING.encode("utf-8"),
    )
    campaign = service.research(as_of=AS_OF)
    assert campaign.summary["news"]["ingested"] is True
    assert campaign.summary["news"]["feeds"] == 3
    assert campaign.summary["news"]["stored"] == 1
    assert any(item.symbol == "RELIANCE" for item in campaign.items)
