from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.data.broker_credentials import (
    BrokerCredentialError,
    BrokerCredentialStore,
)
from bq.data.instrument_master import (
    Instrument,
    InstrumentMasterError,
    InstrumentMasterStore,
    parse_instruments,
)
from bq.data.symbols import Exchange, Symbol
from bq.data.vendors.errors import NoMarketDataError, VendorUnavailableError
from bq.infra.database import migrate

RELIANCE = Symbol(Exchange.NSE, "RELIANCE")


class FrozenClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def _path(tmp_path: Path) -> Path:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return path


def _rows() -> list[dict[str, object]]:
    return [
        {
            "exchange": "NSE",
            "tradingsymbol": "RELIANCE",
            "instrument_token": 738561,
            "lot_size": 1,
            "instrument_type": "EQ",
        },
        {
            "exchange": "NSE",
            "tradingsymbol": "TCS",
            "instrument_token": 2953217,
            "lot_size": 1,
            "instrument_type": "EQ",
        },
        {
            "exchange": "NSE",
            "tradingsymbol": "NIFTY25NOVFUT",
            "instrument_token": 999999,
            "lot_size": 50,
            "instrument_type": "FUT",
        },
        {
            "exchange": "MCX",
            "tradingsymbol": "GOLD",
            "instrument_token": 5,
            "lot_size": 1,
            "instrument_type": "FUT",
        },
    ]


# -- parsing ---------------------------------------------------------------


def test_parse_keeps_equity_rows_only() -> None:
    instruments = parse_instruments(_rows(), exchange="NSE")
    symbols = {item.tradingsymbol for item in instruments}
    assert symbols == {"RELIANCE", "TCS"}


def test_parse_rejects_a_non_mapping_dump() -> None:
    with pytest.raises(InstrumentMasterError):
        parse_instruments({"not": "a list"}, exchange="NSE")


def test_parse_rejects_a_non_mapping_row() -> None:
    with pytest.raises(InstrumentMasterError):
        parse_instruments(["nope"], exchange="NSE")


def test_parse_rejects_a_boolean_token() -> None:
    rows = [
        {
            "exchange": "NSE",
            "tradingsymbol": "X",
            "instrument_token": True,
            "lot_size": 1,
        }
    ]
    with pytest.raises(InstrumentMasterError):
        parse_instruments(rows, exchange="NSE")


def test_instrument_validates_its_own_fields() -> None:
    with pytest.raises(InstrumentMasterError):
        Instrument("NSE", "X", 0, 1)
    with pytest.raises(InstrumentMasterError):
        Instrument("NSE", "X", 1, 0)
    with pytest.raises(InstrumentMasterError):
        Instrument("MCX", "X", 1, 1)


# -- store -----------------------------------------------------------------


def test_missing_cache_is_unavailable_not_empty(tmp_path: Path) -> None:
    store = InstrumentMasterStore(
        _path(tmp_path), FrozenClock(datetime(2026, 11, 25, tzinfo=UTC))
    )
    with pytest.raises(VendorUnavailableError) as raised:
        store.resolve(RELIANCE)
    assert raised.value.reason == "instrument_master_missing"


def test_refresh_then_resolve(tmp_path: Path) -> None:
    store = InstrumentMasterStore(
        _path(tmp_path), FrozenClock(datetime(2026, 11, 25, tzinfo=UTC))
    )
    # Two NSE equities survive the filter: the future and the MCX row are dropped.
    assert store.refresh("NSE", _rows()) == 2
    assert store.resolve(RELIANCE) == Instrument("NSE", "RELIANCE", 738561, 1)


def test_cached_date_reports_the_dump_day(tmp_path: Path) -> None:
    store = InstrumentMasterStore(
        _path(tmp_path), FrozenClock(datetime(2026, 11, 25, 9, 0, tzinfo=UTC))
    )
    store.refresh("NSE", _rows())
    cached = store.cached_date("NSE")
    assert cached is not None and cached.isoformat() == "2026-11-25"
    assert store.cached_date("BSE") is None


def test_unknown_symbol_after_refresh_is_no_data(tmp_path: Path) -> None:
    store = InstrumentMasterStore(
        _path(tmp_path), FrozenClock(datetime(2026, 11, 25, tzinfo=UTC))
    )
    store.refresh("NSE", _rows())
    with pytest.raises(NoMarketDataError):
        store.resolve(Symbol(Exchange.NSE, "NOPE"))


def test_futures_token_is_not_resolvable_as_an_equity(tmp_path: Path) -> None:
    store = InstrumentMasterStore(
        _path(tmp_path), FrozenClock(datetime(2026, 11, 25, tzinfo=UTC))
    )
    store.refresh("NSE", _rows())
    with pytest.raises(NoMarketDataError):
        store.resolve(Symbol(Exchange.NSE, "NIFTY25NOVFUT"))


def test_a_new_day_refreshes_and_keeps_history(tmp_path: Path) -> None:
    path = _path(tmp_path)
    day_one = InstrumentMasterStore(
        path, FrozenClock(datetime(2026, 11, 24, tzinfo=UTC))
    )
    day_one.refresh("NSE", _rows())
    day_two = InstrumentMasterStore(
        path, FrozenClock(datetime(2026, 11, 25, tzinfo=UTC))
    )
    day_one_cached = day_two.cached_date("NSE")
    assert day_one_cached is not None and day_one_cached.isoformat() == "2026-11-24"
    day_two.refresh("NSE", _rows())
    day_two_cached = day_two.cached_date("NSE")
    assert day_two_cached is not None and day_two_cached.isoformat() == "2026-11-25"


def test_a_failed_refresh_leaves_the_previous_day_readable(tmp_path: Path) -> None:
    """Stale tokens must not be served silently *as today's*, but the prior dump
    stays intact so an operator can see what was cached before the failure."""
    path = _path(tmp_path)
    store = InstrumentMasterStore(path, FrozenClock(datetime(2026, 11, 24, tzinfo=UTC)))
    store.refresh("NSE", _rows())
    before = store.resolve(RELIANCE)

    failing = InstrumentMasterStore(
        path, FrozenClock(datetime(2026, 11, 25, tzinfo=UTC))
    )
    with pytest.raises(InstrumentMasterError):
        failing.refresh("NSE", "not a dump")
    # The yesterday dump is still there, and its date is honest about being stale.
    stale = failing.cached_date("NSE")
    assert stale is not None and stale.isoformat() == "2026-11-24"
    assert store.resolve(RELIANCE) == before


def test_resolve_rejects_an_invalid_exchange(tmp_path: Path) -> None:
    store = InstrumentMasterStore(
        _path(tmp_path), FrozenClock(datetime(2026, 11, 25, tzinfo=UTC))
    )
    with pytest.raises(InstrumentMasterError):
        store.refresh("MCX", _rows())


# -- credentials -----------------------------------------------------------


def _credentials(tmp_path: Path) -> BrokerCredentialStore:
    return BrokerCredentialStore(
        _path(tmp_path), FrozenClock(datetime(2026, 11, 25, 12, 0, tzinfo=UTC))
    )


def test_credentials_absent_by_default(tmp_path: Path) -> None:
    store = _credentials(tmp_path)
    assert store.status().configured is False
    assert store.credentials() is None


def test_save_then_status_is_masked(tmp_path: Path) -> None:
    store = _credentials(tmp_path)
    status = store.save(
        api_key="abcdefgh12345678",
        api_secret="secretsecret1234",
        access_token="tokentoken12345",
    )
    assert status.configured is True
    assert status.api_key_masked is not None
    # The masked hint must not contain the full key.
    assert "abcdefgh12345678" not in status.api_key_masked
    assert status.api_key_masked.endswith("5678")


def test_credentials_are_readable_internally(tmp_path: Path) -> None:
    store = _credentials(tmp_path)
    store.save(
        api_key="abcdefgh12345678",
        api_secret="secretsecret1234",
        access_token="tokentoken12345",
    )
    assert store.credentials() == (
        "abcdefgh12345678",
        "secretsecret1234",
        "tokentoken12345",
    )


def test_short_secret_is_rejected(tmp_path: Path) -> None:
    store = _credentials(tmp_path)
    with pytest.raises(BrokerCredentialError):
        store.save(
            api_key="short",
            api_secret="secretsecret1234",
            access_token="tokentoken12345",
        )


def test_whitespace_in_a_secret_is_rejected(tmp_path: Path) -> None:
    store = _credentials(tmp_path)
    with pytest.raises(BrokerCredentialError):
        store.save(
            api_key="abcdefgh12345678",
            api_secret="secret secret 1234",
            access_token="tokentoken12345",
        )


def test_clear_removes_the_credential(tmp_path: Path) -> None:
    store = _credentials(tmp_path)
    store.save(
        api_key="abcdefgh12345678",
        api_secret="secretsecret1234",
        access_token="tokentoken12345",
    )
    store.clear()
    assert store.status().configured is False
    assert store.credentials() is None
