import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import bq.data.vendors.kite_vendor as kite_module
from bq.data.symbols import Exchange, Symbol
from bq.data.vendors import (
    BarInterval,
    DataDomain,
    NoMarketDataError,
    VendorUnavailableError,
)
from bq.data.vendors.kite_vendor import KiteVendor


class TokenException(Exception):
    """Same class name Kite raises for an expired access token."""


class FakeKite:
    def __init__(
        self,
        *,
        quote_error: Exception | None = None,
        history_error: Exception | None = None,
        quote_payload: dict[str, dict[str, object]] | None = None,
        history: list[dict[str, object]] | None = None,
    ) -> None:
        self.quote_error = quote_error
        self.history_error = history_error
        self.quote_payload = quote_payload or {}
        self.history = history or []
        self.history_calls: list[tuple[object, object, object, object]] = []

    def instruments(self, exchange: str | None = None) -> list[dict[str, object]]:
        return []

    def quote(self, *instruments: str) -> dict[str, dict[str, object]]:
        if self.quote_error:
            raise self.quote_error
        return self.quote_payload

    def ohlc(self, *instruments: str) -> dict[str, dict[str, object]]:
        return self.quote_payload

    def historical_data(
        self,
        instrument_token: int,
        from_date: datetime | str,
        to_date: datetime | str,
        interval: str,
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict[str, object]]:
        self.history_calls.append((instrument_token, from_date, to_date, interval))
        if self.history_error:
            raise self.history_error
        return self.history


RELIANCE = Symbol(Exchange.NSE, "RELIANCE")
TOKENS = {("NSE", "RELIANCE"): 738561}


class FixedClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


# -- ADR-013 boundary ------------------------------------------------------


def test_module_exposes_no_order_placing_method() -> None:
    """ADR-013: Kite is read-only. A future edit must not add order methods."""
    forbidden = {
        "place_order",
        "modify_order",
        "cancel_order",
        "place_autoslice_order",
        "convert_position",
    }
    public = {name for name in dir(KiteVendor) if not name.startswith("_")}
    assert public & forbidden == set()


def test_module_source_mentions_no_order_call() -> None:
    source = inspect.getsource(kite_module)
    for call in ("place_order", "modify_order", "cancel_order", "autoslice"):
        assert call not in source, f"{call} must not appear in the read-only vendor"


def test_vendor_advertises_realtime_capability() -> None:
    vendor = KiteVendor(FakeKite(), instrument_tokens=TOKENS)
    assert vendor.is_realtime is True
    assert DataDomain.QUOTES_LIVE in vendor.capabilities


# -- quotes ----------------------------------------------------------------


def _quote_payload(last_price: float = 1351.0) -> dict[str, dict[str, object]]:
    return {
        "NSE:RELIANCE": {
            "last_price": last_price,
            "volume": 1000,
            "last_trade_time": "2026-11-25T10:00:00+05:30",
            "ohlc": {"open": 1340.0, "high": 1360.0, "low": 1335.0, "close": 1345.0},
        }
    }


def test_quote_returns_a_realtime_marked_value() -> None:
    vendor = KiteVendor(
        FakeKite(quote_payload=_quote_payload()), instrument_tokens=TOKENS
    )
    quote = vendor.quote(RELIANCE)
    assert quote.is_realtime is True
    assert quote.last_price == Decimal("1351.0")
    assert quote.day_open == Decimal("1340.0")


def test_expired_token_maps_to_kite_auth_expired() -> None:
    vendor = KiteVendor(
        FakeKite(quote_error=TokenException("token expired")),
        instrument_tokens=TOKENS,
    )
    with pytest.raises(VendorUnavailableError) as raised:
        vendor.quote(RELIANCE)
    assert raised.value.reason == "kite_auth_expired"


def test_generic_transport_error_is_reported_as_transport() -> None:
    vendor = KiteVendor(
        FakeKite(quote_error=OSError("network down")), instrument_tokens=TOKENS
    )
    with pytest.raises(VendorUnavailableError) as raised:
        vendor.quote(RELIANCE)
    assert raised.value.reason == "transport"


def test_unknown_symbol_reports_no_data() -> None:
    vendor = KiteVendor(FakeKite(quote_payload={}), instrument_tokens=TOKENS)
    with pytest.raises(NoMarketDataError):
        vendor.quote(RELIANCE)


def test_index_has_no_kite_symbol_so_it_is_no_data() -> None:
    vendor = KiteVendor(FakeKite(), instrument_tokens=TOKENS)
    with pytest.raises(NoMarketDataError):
        vendor.quote(Symbol(Exchange.INDEX, "NIFTY 50"))


def test_missing_instrument_token_is_unavailable_not_silent() -> None:
    vendor = KiteVendor(FakeKite(), instrument_tokens={})
    with pytest.raises(VendorUnavailableError) as raised:
        vendor.quote(RELIANCE)
    assert raised.value.reason == "instrument_master_missing"


# -- bars ------------------------------------------------------------------


def _history_rows(start: datetime, count: int) -> list[dict[str, object]]:
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.5 + index,
            "volume": 500,
        }
        for index in range(count)
    ]


def test_bars_are_parsed_and_sorted() -> None:
    start = datetime(2026, 11, 20, tzinfo=UTC)
    fake = FakeKite(history=list(reversed(_history_rows(start, 3))))
    vendor = KiteVendor(
        fake,
        instrument_tokens=TOKENS,
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    bars = vendor.bars(
        RELIANCE,
        interval=BarInterval.DAY_1,
        start=start,
        end=datetime(2026, 11, 25, tzinfo=UTC),
    )
    assert [bar.event_at for bar in bars] == sorted(bar.event_at for bar in bars)
    assert len(bars) == 3


def test_historical_requests_are_chunked_by_interval_limit() -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 11, 25, tzinfo=UTC)
    fake = FakeKite(history=_history_rows(start, 1))
    vendor = KiteVendor(
        fake,
        instrument_tokens=TOKENS,
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    vendor.bars(
        RELIANCE,
        interval=BarInterval.MINUTE_1,
        start=start,
        end=end,
    )
    # A ~85-day window against a 60-day limit must be more than one call, and
    # the first call must begin at the requested start, not at the newest window.
    assert len(fake.history_calls) > 1
    assert fake.history_calls[0][1] == start


def test_an_absurd_window_is_refused_rather_than_hammering_the_broker() -> None:
    vendor = KiteVendor(
        FakeKite(history=[]),
        instrument_tokens=TOKENS,
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    with pytest.raises(ValueError):
        vendor.bars(
            RELIANCE,
            interval=BarInterval.MINUTE_1,
            start=datetime(2020, 1, 1, tzinfo=UTC),
            end=datetime(2026, 11, 25, tzinfo=UTC),
        )


def test_history_token_expiry_maps_to_auth_expired() -> None:
    vendor = KiteVendor(
        FakeKite(history_error=TokenException("expired")),
        instrument_tokens=TOKENS,
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    with pytest.raises(VendorUnavailableError) as raised:
        vendor.bars(
            RELIANCE,
            interval=BarInterval.DAY_1,
            start=datetime(2026, 11, 1, tzinfo=UTC),
            end=datetime(2026, 11, 25, tzinfo=UTC),
        )
    assert raised.value.reason == "kite_auth_expired"


def test_empty_history_reports_no_data() -> None:
    empty_start = datetime(2026, 11, 1, tzinfo=UTC)
    vendor = KiteVendor(
        FakeKite(history=[]),
        instrument_tokens=TOKENS,
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    with pytest.raises(NoMarketDataError):
        vendor.bars(
            RELIANCE,
            interval=BarInterval.DAY_1,
            start=empty_start,
            end=datetime(2026, 11, 25, tzinfo=UTC),
        )


def test_malformed_history_rows_are_skipped_not_fatal() -> None:
    start = datetime(2026, 11, 20, tzinfo=UTC)
    rows = _history_rows(start, 2)
    rows.append({"date": "not-a-date", "open": 1, "close": 1})
    rows.append(
        {
            "date": start.isoformat(),
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume": 1,
        }
    )
    vendor = KiteVendor(
        FakeKite(history=rows),
        instrument_tokens=TOKENS,
        clock=FixedClock(datetime(2026, 11, 25, tzinfo=UTC)),
    )
    bars = vendor.bars(
        RELIANCE,
        interval=BarInterval.DAY_1,
        start=start,
        end=datetime(2026, 11, 25, tzinfo=UTC),
    )
    assert len(bars) == 2
