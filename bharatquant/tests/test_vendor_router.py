import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from bq.data.symbols import Exchange, Symbol, resolve
from bq.data.vendors import (
    Bar,
    BarInterval,
    DataDomain,
    NoMarketDataError,
    Quote,
    RoutingError,
    VendorError,
    VendorNotRegisteredError,
    VendorRouter,
    VendorUnavailableError,
)
from bq.data.vendors.yahoo_vendor import YahooVendor

RELIANCE = Symbol(Exchange.NSE, "RELIANCE")


class FakeVendor:
    """A vendor whose behaviour each test dictates."""

    is_realtime = False

    def __init__(
        self,
        name: str,
        *,
        capabilities: frozenset[DataDomain] | None = None,
        error: Exception | None = None,
        realtime: bool = False,
        bars: tuple[Bar, ...] = (),
    ) -> None:
        self.name = name
        self.capabilities = capabilities or frozenset(
            {DataDomain.QUOTES_LIVE, DataDomain.BARS_DAILY}
        )
        self.is_realtime = realtime
        self._error = error
        self._bars = bars
        self.calls = 0

    def quote(self, symbol: Symbol) -> Quote:
        self.calls += 1
        if self._error:
            raise self._error
        return Quote(
            symbol=symbol,
            last_price=Decimal("100.00"),
            as_of=datetime(2026, 11, 24, 10, 0, tzinfo=UTC),
            is_realtime=self.is_realtime,
            delay_seconds=0.0 if self.is_realtime else 900.0,
        )

    def bars(
        self,
        symbol: Symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> tuple[Bar, ...]:
        self.calls += 1
        if self._error:
            raise self._error
        return self._bars


def _bar(at: datetime, close: str = "100") -> Bar:
    price = Decimal(close)
    return Bar(
        event_at=at,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=10,
    )


# -- registration ---------------------------------------------------------


def test_register_vendor_is_the_only_extension_point() -> None:
    router = VendorRouter()
    vendor = FakeVendor("custom")
    router.register_vendor(DataDomain.QUOTES_LIVE, vendor)
    assert router.chain(DataDomain.QUOTES_LIVE) == (vendor,)
    # The core file was not edited to add it.


def test_register_rejects_a_vendor_that_cannot_claim_the_domain() -> None:
    router = VendorRouter()
    vendor = FakeVendor("yahoo", capabilities=frozenset({DataDomain.BARS_DAILY}))
    with pytest.raises(VendorError):
        router.register_vendor(DataDomain.QUOTES_LIVE, vendor)


def test_register_with_priority_orders_the_chain() -> None:
    router = VendorRouter()
    first = FakeVendor("first")
    second = FakeVendor("second")
    router.register_vendor(DataDomain.QUOTES_LIVE, second)
    router.register_vendor(DataDomain.QUOTES_LIVE, first, priority=0)
    assert [v.name for v in router.chain(DataDomain.QUOTES_LIVE)] == [
        "first",
        "second",
    ]


def test_register_is_idempotent_for_the_same_vendor() -> None:
    router = VendorRouter()
    vendor = FakeVendor("only")
    router.register_vendor(DataDomain.QUOTES_LIVE, vendor)
    router.register_vendor(DataDomain.QUOTES_LIVE, vendor)
    assert len(router.chain(DataDomain.QUOTES_LIVE)) == 1


# -- fallback -------------------------------------------------------------


def test_first_vendor_wins_and_provenance_is_reported() -> None:
    router = VendorRouter()
    primary = FakeVendor("kite", realtime=True)
    backup = FakeVendor("yahoo")
    router.register_vendor(DataDomain.QUOTES_LIVE, primary)
    router.register_vendor(DataDomain.QUOTES_LIVE, backup)

    result = router.quote(RELIANCE)
    assert result.source == "kite"
    assert result.is_realtime is True
    assert backup.calls == 0


def test_auth_expiry_falls_through_to_the_next_vendor() -> None:
    router = VendorRouter()
    kite = FakeVendor(
        "kite", error=VendorUnavailableError("kite_auth_expired"), realtime=True
    )
    yahoo = FakeVendor("yahoo", realtime=False)
    router.register_vendor(DataDomain.QUOTES_LIVE, kite)
    router.register_vendor(DataDomain.QUOTES_LIVE, yahoo)

    result = router.quote(RELIANCE)
    assert result.source == "yahoo"
    # The fallback is honest about being delayed.
    assert result.is_realtime is False


def test_every_vendor_failing_raises_with_per_vendor_reasons() -> None:
    router = VendorRouter()
    router.register_vendor(
        DataDomain.QUOTES_LIVE,
        FakeVendor("kite", error=VendorUnavailableError("kite_auth_expired")),
    )
    router.register_vendor(
        DataDomain.QUOTES_LIVE,
        FakeVendor("yahoo", error=VendorUnavailableError("rate_limited")),
    )
    with pytest.raises(RoutingError) as raised:
        router.quote(RELIANCE)
    assert raised.value.failures == (
        ("kite", "kite_auth_expired"),
        ("yahoo", "rate_limited"),
    )
    assert "kite_auth_expired" in str(raised.value)
    assert "rate_limited" in str(raised.value)


def test_all_no_data_is_reported_as_no_data_not_an_outage() -> None:
    router = VendorRouter()
    router.register_vendor(
        DataDomain.QUOTES_LIVE,
        FakeVendor("kite", error=NoMarketDataError("NSE:RELIANCE")),
    )
    router.register_vendor(
        DataDomain.QUOTES_LIVE,
        FakeVendor("yahoo", error=NoMarketDataError("NSE:RELIANCE")),
    )
    with pytest.raises(NoMarketDataError):
        router.quote(RELIANCE)


def test_empty_chain_is_a_distinct_error() -> None:
    with pytest.raises(VendorNotRegisteredError):
        VendorRouter().quote(RELIANCE)


def test_a_vendor_bug_does_not_masquerade_as_an_outage() -> None:
    class Broken(FakeVendor):
        def quote(self, symbol: Symbol) -> Quote:
            raise KeyError("boom")

    router = VendorRouter()
    router.register_vendor(DataDomain.QUOTES_LIVE, Broken("broken"))
    router.register_vendor(DataDomain.QUOTES_LIVE, FakeVendor("yahoo"))
    result = router.quote(RELIANCE)
    assert result.source == "yahoo"


# -- bar routing ----------------------------------------------------------


def test_daily_interval_routes_to_the_daily_domain() -> None:
    router = VendorRouter()
    daily = FakeVendor("yahoo")
    router.register_vendor(DataDomain.BARS_DAILY, daily)
    start = datetime(2026, 11, 20, tzinfo=UTC)
    end = start + timedelta(days=3)
    router.bars(
        RELIANCE,
        interval=BarInterval.DAY_1,
        start=start,
        end=end,
    )
    assert daily.calls == 1


def test_intraday_interval_routes_to_the_intraday_domain() -> None:
    router = VendorRouter()
    intraday = FakeVendor("kite", capabilities=frozenset({DataDomain.BARS_INTRADAY}))
    router.register_vendor(DataDomain.BARS_INTRADAY, intraday)
    start = datetime(2026, 11, 24, 9, 15, tzinfo=UTC)
    result = router.bars(
        RELIANCE,
        interval=BarInterval.MINUTE_15,
        start=start,
        end=start + timedelta(hours=6),
    )
    assert result.source == "kite"


def test_bar_bounds_must_be_ordered_and_aware() -> None:
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, FakeVendor("yahoo"))
    naive = datetime(2026, 11, 20)
    with pytest.raises(ValueError):
        router.bars(RELIANCE, interval=BarInterval.DAY_1, start=naive, end=naive)
    aware = datetime(2026, 11, 20, tzinfo=UTC)
    with pytest.raises(ValueError):
        router.bars(
            RELIANCE,
            interval=BarInterval.DAY_1,
            start=aware,
            end=aware - timedelta(days=1),
        )


# -- yahoo vendor ---------------------------------------------------------


def _chart_payload(
    *, timestamps: list[int], closes: list[object], symbol: str = "RELIANCE.NS"
) -> bytes:
    n = len(timestamps)
    return json.dumps(
        {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": symbol, "currency": "INR"},
                        "timestamp": timestamps,
                        "indicators": {
                            "quote": [
                                {
                                    "open": closes,
                                    "high": [
                                        c if c is not None else None for c in closes
                                    ],
                                    "low": [
                                        c if c is not None else None for c in closes
                                    ],
                                    "close": closes,
                                    "volume": [100] * n,
                                }
                            ]
                        },
                    }
                ],
            }
        }
    ).encode()


def _transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


class FixedClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def test_yahoo_vendor_declares_itself_delayed() -> None:
    vendor = YahooVendor()
    assert vendor.is_realtime is False
    assert DataDomain.QUOTES_LIVE in vendor.capabilities
    assert DataDomain.INDICES in vendor.capabilities


def test_yahoo_vendor_returns_bars_from_a_payload() -> None:
    base = int(datetime(2026, 11, 20, tzinfo=UTC).timestamp())
    payload = _chart_payload(timestamps=[base, base + 86400], closes=[100.0, 101.5])
    vendor = YahooVendor(
        transport=_transport(lambda request: httpx.Response(200, content=payload)),
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    bars = vendor.bars(
        RELIANCE,
        interval=BarInterval.DAY_1,
        start=datetime(2026, 11, 1, tzinfo=UTC),
        end=datetime(2026, 11, 25, tzinfo=UTC),
    )
    assert len(bars) == 2
    assert bars[-1].close == Decimal("101.5")


def test_yahoo_vendor_maps_429_to_rate_limited() -> None:
    vendor = YahooVendor(
        transport=_transport(lambda request: httpx.Response(429)),
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    with pytest.raises(VendorUnavailableError) as raised:
        vendor.bars(
            RELIANCE,
            interval=BarInterval.DAY_1,
            start=datetime(2026, 11, 1, tzinfo=UTC),
            end=datetime(2026, 11, 25, tzinfo=UTC),
        )
    assert raised.value.reason == "rate_limited"


def test_yahoo_vendor_reports_no_data_for_an_empty_series() -> None:
    payload = json.dumps({"chart": {"error": None, "result": []}}).encode()
    vendor = YahooVendor(
        transport=_transport(lambda request: httpx.Response(200, content=payload)),
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    with pytest.raises(NoMarketDataError):
        vendor.bars(
            RELIANCE,
            interval=BarInterval.DAY_1,
            start=datetime(2026, 11, 1, tzinfo=UTC),
            end=datetime(2026, 11, 25, tzinfo=UTC),
        )


def test_yahoo_vendor_refuses_a_window_beyond_its_lookback() -> None:
    vendor = YahooVendor(
        transport=_transport(lambda request: httpx.Response(200, content=b"{}")),
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    with pytest.raises(ValueError):
        vendor.bars(
            RELIANCE,
            interval=BarInterval.MINUTE_1,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 11, 25, tzinfo=UTC),
        )


def test_yahoo_vendor_quote_is_delayed_and_labelled() -> None:
    stamp = int(datetime(2026, 11, 24, tzinfo=UTC).timestamp())
    payload = _chart_payload(timestamps=[stamp], closes=[1351.0])
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    vendor = YahooVendor(
        transport=_transport(lambda request: httpx.Response(200, content=payload)),
        clock=FixedClock(now),
    )
    quote = vendor.quote(RELIANCE)
    assert quote.is_realtime is False
    assert quote.last_price == Decimal("1351.0")
    assert quote.delay_seconds >= 900.0


def test_yahoo_vendor_index_symbol_uses_caret_ticker() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        stamp = int(datetime(2026, 11, 24, tzinfo=UTC).timestamp())
        return httpx.Response(
            200,
            content=_chart_payload(
                timestamps=[stamp], closes=[24000.0], symbol="^NSEI"
            ),
        )

    vendor = YahooVendor(
        transport=_transport(handler),
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    vendor.quote(resolve("NIFTY 50"))
    assert any("%5ENSEI" in url or "^NSEI" in url for url in seen)
