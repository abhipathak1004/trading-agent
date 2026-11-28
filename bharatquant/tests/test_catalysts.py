from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from bq.catalysts import (
    AliasIndex,
    Catalyst,
    CatalystEvent,
    CatalystStore,
    extract,
    match_symbols,
    parse_amount_crore,
)
from bq.data.equity_master import EquityAlias
from bq.infra.database import migrate
from bq.news import NewsStore
from bq.shortlist import (
    Liquidity,
    ShortlistConfig,
    ShortlistError,
    build_shortlist,
)

TRADE_DATE = date(2026, 11, 26)
NOW = datetime(2026, 11, 26, 4, 0, tzinfo=UTC)

ALIASES = (
    EquityAlias(key="reliance", symbol="RELIANCE", kind="name"),
    EquityAlias(key="sun pharmaceutical", symbol="SUNPHARMA", kind="name"),
    EquityAlias(key="bank of baroda", symbol="BANKBARODA", kind="name"),
    EquityAlias(key="RELIANCE", symbol="RELIANCE", kind="ticker"),
    EquityAlias(key="SUNPHARMA", symbol="SUNPHARMA", kind="ticker"),
    EquityAlias(key="BANKBARODA", symbol="BANKBARODA", kind="ticker"),
    EquityAlias(key="ORDER", symbol="BOGUS", kind="ticker"),
    EquityAlias(key="TAKE", symbol="TAKE", kind="ticker"),
    EquityAlias(key="wealth", symbol="WEALTH", kind="name"),
    EquityAlias(key="WEALTH", symbol="WEALTH", kind="ticker"),
    EquityAlias(key="alembic pharma", symbol="ALEMBICLTD", kind="name"),
    EquityAlias(key="standard", symbol="SIL", kind="name"),
    EquityAlias(key="SIEMENS", symbol="SIEMENS", kind="ticker"),
    EquityAlias(key="siemens energy india", symbol="ENRIN", kind="name"),
    EquityAlias(key="ptc industries", symbol="PTCIL", kind="name"),
)
INDEX = AliasIndex.from_aliases(ALIASES)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _item(
    title: str,
    *,
    summary: str = "",
    news_id: str = "news-1",
    category: str = "CORPORATE",
    published_at: datetime = NOW,
):
    from bq.news import NewsCategory, NewsItem

    return NewsItem(
        news_id=news_id,
        source_id="pulse.zerodha",
        url="https://example.com/story",
        title=title,
        summary=summary,
        category=NewsCategory(category),
        long_term_impact=False,
        published_at=published_at,
        ingested_at=published_at,
    )


def test_symbol_matching_handles_names_and_tickers() -> None:
    assert match_symbols("Reliance Industries wins a large order", INDEX) == (
        "RELIANCE",
    )
    assert match_symbols("Sun Pharmaceutical Q2 profit rises", INDEX) == ("SUNPHARMA",)
    assert match_symbols("TCS and BANKBARODA both gained", INDEX) == ("BANKBARODA",)
    assert match_symbols("ORDER stands alone", INDEX) == ()


def test_amount_parsing_is_bounded_to_crore() -> None:
    from decimal import Decimal

    assert parse_amount_crore("wins ₹2,000 crore order") == Decimal("2000")
    assert parse_amount_crore("Rs 450 cr contract") == Decimal("450")
    assert parse_amount_crore("1.5 lakh shares") == Decimal("0.015")
    assert parse_amount_crore("no money here") == Decimal("0")


def test_order_win_is_positive_and_scales_with_money() -> None:
    item = _item("Reliance wins ₹2,000 crore order from ONGC")
    catalysts = extract(item, INDEX, trade_date=TRADE_DATE)
    assert len(catalysts) == 1
    catalyst = catalysts[0]
    assert catalyst.event is CatalystEvent.ORDER_WIN
    assert catalyst.polarity == 1
    assert catalyst.strength == 5


def test_cancelled_order_is_negative_not_a_win() -> None:
    item = _item("Reliance order cancelled by ONGC")
    events = [row.event for row in extract(item, INDEX, trade_date=TRADE_DATE)]
    assert events == [CatalystEvent.ORDER_LOSS]


def test_profit_fall_is_weak_results_not_strong() -> None:
    item = _item("Sun Pharmaceutical Q2 profit falls 20%")
    events = [row.event for row in extract(item, INDEX, trade_date=TRADE_DATE)]
    assert events == [CatalystEvent.EARNINGS_WEAK]


def test_bare_company_mention_stays_neutral_and_weak() -> None:
    item = _item("Reliance Industries board meeting on 30 November")
    catalyst = extract(item, INDEX, trade_date=TRADE_DATE)[0]
    assert catalyst.event is CatalystEvent.CORPORATE_OTHER
    assert catalyst.strength == 1


def test_macro_news_never_becomes_a_symbol_catalyst() -> None:
    item = _item("Reliance helps Nifty close higher", category="INDIA_MARKET")
    assert extract(item, INDEX, trade_date=TRADE_DATE) == ()


def test_store_round_trip_and_unmatched_coverage(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    news = NewsStore(path, FrozenClock())
    news.ingest(
        [
            _feed("Reliance wins ₹2,000 crore order from ONGC"),
            _feed("Board approves dividend", url="https://example.com/two"),
        ]
    )
    items = news.recent(limit=10)
    catalysts = tuple(
        catalyst
        for item in items
        for catalyst in extract(item, INDEX, trade_date=TRADE_DATE)
    )
    store = CatalystStore(path, FrozenClock())
    assert store.record(catalysts, trade_date=TRADE_DATE) == 1
    assert store.record(catalysts, trade_date=TRADE_DATE) == 0
    stored = store.for_date(TRADE_DATE)
    assert [row.symbol for row in stored] == ["RELIANCE"]
    assert stored[0].title == "Reliance wins ₹2,000 crore order from ONGC"
    assert store.unmatched_corporate_count(TRADE_DATE) == 1


def _feed(title: str, *, url: str = "https://example.com/one"):
    from bq.news import FeedEntry

    return FeedEntry(
        title=title,
        summary="",
        url=url,
        published_at=datetime(2026, 11, 26, 4, 0, tzinfo=UTC),
    )


def _stored(symbol: str, event: CatalystEvent, strength: int, *, news="n1"):
    from bq.catalysts import StoredCatalyst

    return StoredCatalyst(
        catalyst_id=f"{symbol}-{event.value}-{news}",
        news_id=news,
        symbol=symbol,
        event=event,
        polarity=1
        if event
        in {
            CatalystEvent.ORDER_WIN,
            CatalystEvent.EARNINGS_STRONG,
        }
        else -1,
        strength=strength,
        matched_terms=("test",),
        trade_date=TRADE_DATE,
        extracted_at=NOW,
        title=f"{symbol} {event.value}",
        published_at=NOW,
    )


def test_shortlist_ranks_positive_and_drops_hard_negatives() -> None:
    rows = (
        _stored("RELIANCE", CatalystEvent.ORDER_WIN, 5),
        _stored("SUNPHARMA", CatalystEvent.EARNINGS_STRONG, 4),
        _stored("BANKBARODA", CatalystEvent.ORDER_WIN, 3),
        _stored("TCS", CatalystEvent.ORDER_LOSS, 4),
        _stored("INFY", CatalystEvent.CORPORATE_OTHER, 1),
    )
    shortlist = build_shortlist(
        rows,
        trade_date=TRADE_DATE,
        config=ShortlistConfig(top_n=8),
    )
    assert [item.symbol for item in shortlist.items] == [
        "RELIANCE",
        "SUNPHARMA",
        "BANKBARODA",
    ]
    assert shortlist.insufficient is True
    assert shortlist.considered == 5


def test_shortlist_applies_liquidity_and_size_caps() -> None:
    rows = tuple(
        _stored(f"SYM{index}", CatalystEvent.ORDER_WIN, 5) for index in range(12)
    )
    shortlist = build_shortlist(
        rows,
        trade_date=TRADE_DATE,
        config=ShortlistConfig(top_n=10),
        liquidity={
            "SYM0": Liquidity(last_close_paise=100, avg_volume_20d=1_000_000),
            "SYM1": Liquidity(last_close_paise=5000, avg_volume_20d=10),
        },
    )
    assert len(shortlist.items) == 10
    assert {item.symbol for item in shortlist.items}.isdisjoint({"SYM0", "SYM1"})


def test_learned_weights_can_reorder_catalysts() -> None:
    rows = (
        _stored("A", CatalystEvent.ORDER_WIN, 3),
        _stored("B", CatalystEvent.EARNINGS_STRONG, 3),
    )
    shortlist = build_shortlist(
        rows,
        trade_date=TRADE_DATE,
        config=ShortlistConfig(top_n=2),
        event_weights={
            CatalystEvent.ORDER_WIN: 0.5,
            CatalystEvent.EARNINGS_STRONG: 1.5,
        },
    )
    assert [item.symbol for item in shortlist.items] == ["B", "A"]


def test_shortlist_rejects_an_invalid_top_n() -> None:
    with pytest.raises(ShortlistError, match="top_n"):
        ShortlistConfig(top_n=0)


def test_catalyst_strength_is_bounded() -> None:
    row = Catalyst(
        news_id="n1",
        symbol="RELIANCE",
        event=CatalystEvent.ORDER_WIN,
        polarity=1,
        strength=5,
        matched_terms=("wins order",),
    )
    assert 1 <= row.strength <= 5

    older = _item(
        "Reliance wins order",
        published_at=NOW - timedelta(days=3),
    )
    assert extract(older, INDEX, trade_date=TRADE_DATE)[0].strength == 3


def test_tickers_match_only_uppercase_tokens() -> None:
    assert match_symbols("take 1-2 months to settle", INDEX) == ()
    assert match_symbols("TAKE reports results", INDEX) == ("TAKE",)
    assert match_symbols("wealth managers gained", INDEX) == ()


def test_fda_approval_is_a_positive_catalyst() -> None:
    item = _item("Alembic Pharma gets U.S. FDA nod for cancer drug")
    catalyst = extract(item, INDEX, trade_date=TRADE_DATE)[0]
    assert catalyst.event is CatalystEvent.REGULATORY_APPROVAL
    assert catalyst.polarity == 1
    assert catalyst.strength >= 3


def test_summary_only_mention_is_not_attributed() -> None:
    item = _item(
        "Banking stocks rebound on earnings strength",
        summary="ICICI Bank, Reliance and Sun Pharmaceutical gained.",
    )
    assert extract(item, INDEX, trade_date=TRADE_DATE) == ()


def test_publisher_suffix_is_not_a_company() -> None:
    assert match_symbols(
        "L&T wins order worth up to ₹2,500 cr - Business Standard", INDEX
    ) == ("LT",)


def test_longer_subsidiary_name_beats_the_parent_ticker() -> None:
    assert match_symbols("SIEMENS ENERGY INDIA Q2 Results", INDEX) == ("ENRIN",)


def test_order_win_noun_phrase_is_recognised() -> None:
    item = _item("PTC Industries rebounds on major defence order win")
    events = [row.event for row in extract(item, INDEX, trade_date=TRADE_DATE)]
    assert CatalystEvent.ORDER_WIN in events


def test_roundup_listicles_are_skipped() -> None:
    item = _item("Eternal, Adani Enterprises among 10 stocks with highest FII buying")
    assert extract(item, INDEX, trade_date=TRADE_DATE) == ()


def test_clear_reliance_abbreviation_is_recognised() -> None:
    item = _item("RIL wins order worth ₹1,000 crore")
    symbols = {row.symbol for row in extract(item, INDEX, trade_date=TRADE_DATE)}
    assert symbols == {"RELIANCE"}
