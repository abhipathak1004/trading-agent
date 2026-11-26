from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from bq.campaign import Campaign, CampaignService
from bq.core import EXCHANGE_TIMEZONE, CurrencyPaise
from bq.data.vendors.base import Bar, BarInterval
from bq.data.vendors.router import VendorResult
from bq.infra.database import migrate
from bq.news import FeedEntry, NewsStore
from bq.paper_accounts import PaperAccountStore
from bq.settings import SettingsStore
from bq.triggers import TriggerEngine

AS_OF = datetime(2026, 11, 26, 3, 45, tzinfo=UTC)  # 09:15 IST
SESSION_DATE = date(2026, 11, 26)
SCAN = datetime(2026, 11, 26, 4, 30, tzinfo=UTC)  # 10:00 IST
LATE_SCAN = datetime(2026, 11, 26, 9, 30, tzinfo=UTC)  # 15:00 IST

LISTING = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, "
    "MARKET LOT, ISIN NUMBER, FACE VALUE\n"
    "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
)


class FrozenClock:
    def now(self) -> datetime:
        return AS_OF


class FakeRouter:
    def __init__(
        self,
        daily: dict[str, tuple[Bar, ...]],
        session: dict[str, tuple[Bar, ...]] | None = None,
    ) -> None:
        self.daily = daily
        self.session = session or {}

    def bars(
        self,
        symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
        domain=None,
    ):
        source = self.daily if interval is BarInterval.DAY_1 else self.session
        data = tuple(
            bar
            for bar in source.get(symbol.canonical, ())
            if start <= bar.event_at <= end
        )
        return VendorResult(data=data, source="fake", is_realtime=False)


def _daily_bars() -> tuple[Bar, ...]:
    bars: list[Bar] = []
    price = Decimal("1000")
    cursor = SESSION_DATE - timedelta(days=200)
    while cursor < SESSION_DATE:
        if cursor.weekday() < 5:
            close = price + Decimal("5")
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


def _session_bar(
    minute: int,
    *,
    session_date: date = SESSION_DATE,
    open_paise: int,
    high_paise: int,
    low_paise: int,
    close_paise: int,
) -> Bar:
    stamp = datetime.combine(
        session_date, time(9, 15), tzinfo=EXCHANGE_TIMEZONE
    ) + timedelta(minutes=minute)
    return Bar(
        event_at=stamp.astimezone(UTC),
        open=Decimal(open_paise) / Decimal(100),
        high=Decimal(high_paise) / Decimal(100),
        low=Decimal(low_paise) / Decimal(100),
        close=Decimal(close_paise) / Decimal(100),
        volume=100_000,
    )


def _setup(tmp_path: Path) -> tuple[Campaign, Path, int, int]:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    NewsStore(path, FrozenClock()).ingest(
        [
            FeedEntry(
                title="Reliance wins ₹2,000 crore order from ONGC",
                summary="",
                url="https://example.com/one",
                published_at=AS_OF - timedelta(hours=2),
            )
        ]
    )
    PaperAccountStore(path, FrozenClock()).create(
        account_id="paper-main",
        name="Paper main",
        opening_balance_paise=CurrencyPaise(100_000_000),
    )
    daily = {"NSE:RELIANCE": _daily_bars()}
    service = CampaignService(
        path,
        SettingsStore(path),
        clock=FrozenClock(),
        router=FakeRouter(daily),
        news_fetcher=lambda _url: b"",
        listing_fetcher=lambda: LISTING.encode("utf-8"),
    )
    campaign = service.research(as_of=AS_OF)
    reliance = next(item for item in campaign.items if item.symbol == "RELIANCE")
    trigger = reliance.trigger
    assert trigger is not None
    return campaign, path, int(trigger["level_paise"]), int(trigger["stop_paise"])


def _engine(path: Path, session: tuple[Bar, ...]) -> TriggerEngine:
    return TriggerEngine(
        path,
        SettingsStore(path),
        clock=FrozenClock(),
        router=FakeRouter({"NSE:RELIANCE": _daily_bars()}, {"NSE:RELIANCE": session}),
    )


def _row(path: Path, sql: str) -> int:
    import sqlite3

    with sqlite3.connect(path) as connection:
        return int(connection.execute(sql).fetchone()[0])


def test_breakout_fills_through_the_paper_gate(tmp_path: Path) -> None:
    campaign, path, level, _stop = _setup(tmp_path)
    session = (
        _session_bar(
            40,
            open_paise=level - 100,
            high_paise=level + 600,
            low_paise=level - 200,
            close_paise=level + 500,
        ),
    )
    result = _engine(path, session).scan(as_of=SCAN)
    assert result.session_open is True
    assert result.session_source == "weekday_fallback"
    assert result.entries_triggered == 1

    updated = _engine(path, session).store.get(campaign.campaign_id)
    reliance = next(item for item in updated.items if item.symbol == "RELIANCE")
    assert reliance.status == "FILLED"
    assert reliance.outcome is not None
    assert reliance.outcome["state"] == "FILLED"
    assert reliance.outcome["entry_paise"] == level + 500
    assert reliance.outcome["quantity"] > 0
    assert _row(path, "SELECT COUNT(*) FROM paper_fills") == 1


def test_gap_above_the_chase_band_is_not_chased(tmp_path: Path) -> None:
    campaign, path, level, _stop = _setup(tmp_path)
    session = (
        _session_bar(
            40,
            open_paise=level + level * 100 // 10_000,
            high_paise=level + level * 140 // 10_000,
            low_paise=level,
            close_paise=level + level * 120 // 10_000,
        ),
    )
    result = _engine(path, session).scan(as_of=SCAN)
    assert result.entries_blocked == 1
    updated = _engine(path, session).store.get(campaign.campaign_id)
    reliance = next(item for item in updated.items if item.symbol == "RELIANCE")
    assert reliance.status == "MISSED_CHASE"
    assert _row(path, "SELECT COUNT(*) FROM paper_orders") == 0


def test_scan_after_valid_until_expires_the_watch(tmp_path: Path) -> None:
    campaign, path, level, _stop = _setup(tmp_path)
    session = (
        _session_bar(
            300,
            open_paise=level,
            high_paise=level + 500,
            low_paise=level - 100,
            close_paise=level + 300,
        ),
    )
    result = _engine(path, session).scan(as_of=LATE_SCAN)
    assert result.expired == 1
    updated = _engine(path, session).store.get(campaign.campaign_id)
    reliance = next(item for item in updated.items if item.symbol == "RELIANCE")
    assert reliance.status == "EXPIRED"


def test_intraday_stop_closes_the_position(tmp_path: Path) -> None:
    campaign, path, level, stop = _setup(tmp_path)
    entry_bar = _session_bar(
        40,
        open_paise=level - 100,
        high_paise=level + 600,
        low_paise=level - 200,
        close_paise=level + 500,
    )
    _engine(path, (entry_bar,)).scan(as_of=SCAN)

    stop_bar = _session_bar(
        60,
        session_date=date(2026, 11, 27),
        open_paise=stop + 300,
        high_paise=stop + 400,
        low_paise=stop - 200,
        close_paise=stop - 100,
    )
    later = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)  # 10:30 IST next day
    result = _engine(path, (entry_bar, stop_bar)).scan(as_of=later)
    assert result.exits == 1

    updated = _engine(path, (entry_bar, stop_bar)).store.get(campaign.campaign_id)
    reliance = next(item for item in updated.items if item.symbol == "RELIANCE")
    assert reliance.status == "CLOSED"
    assert reliance.outcome is not None
    assert reliance.outcome["exit_reason"] == "stop"
    assert reliance.r_multiple is not None
    assert reliance.r_multiple < 0


def test_scan_is_idempotent(tmp_path: Path) -> None:
    _campaign, path, level, _stop = _setup(tmp_path)
    session = (
        _session_bar(
            40,
            open_paise=level - 100,
            high_paise=level + 600,
            low_paise=level - 200,
            close_paise=level + 500,
        ),
    )
    engine = _engine(path, session)
    engine.scan(as_of=SCAN)
    engine.scan(as_of=SCAN)
    assert _row(path, "SELECT COUNT(*) FROM paper_orders") == 1
    assert _row(path, "SELECT COUNT(*) FROM paper_fills") == 1


def test_missing_paper_account_blocks_instead_of_crashing(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    NewsStore(path, FrozenClock()).ingest(
        [
            FeedEntry(
                title="Reliance wins ₹2,000 crore order from ONGC",
                summary="",
                url="https://example.com/one",
                published_at=AS_OF - timedelta(hours=2),
            )
        ]
    )
    service = CampaignService(
        path,
        SettingsStore(path),
        clock=FrozenClock(),
        router=FakeRouter({"NSE:RELIANCE": _daily_bars()}),
        news_fetcher=lambda _url: b"",
        listing_fetcher=lambda: LISTING.encode("utf-8"),
    )
    campaign = service.research(as_of=AS_OF)
    level = int(
        next(
            item.trigger["level_paise"]  # type: ignore[index]
            for item in campaign.items
            if item.symbol == "RELIANCE"
        )
    )
    session = (
        _session_bar(
            40,
            open_paise=level - 100,
            high_paise=level + 600,
            low_paise=level - 200,
            close_paise=level + 500,
        ),
    )
    result = _engine(path, session).scan(as_of=SCAN)
    assert result.entries_blocked == 1
    assert _row(path, "SELECT COUNT(*) FROM paper_orders") == 0
