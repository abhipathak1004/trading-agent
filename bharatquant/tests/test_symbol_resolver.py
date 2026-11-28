import pytest

from bq.data.symbols import (
    Exchange,
    Symbol,
    SymbolError,
    parse_yahoo,
    resolve,
    to_kite,
    to_yahoo,
)


def test_bare_ticker_defaults_to_nse() -> None:
    assert resolve("RELIANCE") == Symbol(Exchange.NSE, "RELIANCE")


def test_case_and_surrounding_space_are_normalised() -> None:
    assert resolve("  reliance  ") == Symbol(Exchange.NSE, "RELIANCE")


def test_explicit_exchange_prefix() -> None:
    assert resolve("BSE:RELIANCE") == Symbol(Exchange.BSE, "RELIANCE")
    assert resolve("nse:tcs") == Symbol(Exchange.NSE, "TCS")


def test_yahoo_suffix_input_is_accepted() -> None:
    assert resolve("RELIANCE.NS") == Symbol(Exchange.NSE, "RELIANCE")
    assert resolve("RELIANCE.BO") == Symbol(Exchange.BSE, "RELIANCE")


def test_yahoo_round_trip() -> None:
    for text in ("RELIANCE", "BSE:INFY", "HDFCBANK.NS"):
        symbol = resolve(text)
        assert parse_yahoo(to_yahoo(symbol)) == symbol


@pytest.mark.parametrize(
    ("name", "caret"),
    [
        ("NIFTY 50", "^NSEI"),
        ("nifty50", "^NSEI"),
        ("NIFTY BANK", "^NSEBANK"),
        ("BANKNIFTY", "^NSEBANK"),
        ("SENSEX", "^BSESN"),
        ("INDIA VIX", "^INDIAVIX"),
    ],
)
def test_index_names_map_to_yahoo_carets(name: str, caret: str) -> None:
    symbol = resolve(name)
    assert symbol.exchange is Exchange.INDEX
    assert symbol.tradeable is False
    assert to_yahoo(symbol) == caret


def test_index_has_no_kite_symbol() -> None:
    with pytest.raises(SymbolError):
        to_kite(resolve("NIFTY 50"))


def test_kite_format_for_equities() -> None:
    assert to_kite(resolve("RELIANCE")) == ("NSE", "RELIANCE")
    assert to_kite(resolve("BSE:ITC")) == ("BSE", "ITC")


def test_canonical_round_trip_through_resolve() -> None:
    symbol = resolve("BSE:ITC")
    assert resolve(symbol.canonical) == symbol


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "A" * 25,
        "RELIANCE NS",
        "RELIANCE/NS",
        "NSE:",
        "NASDAQ:RELIANCE",
        "INDEX:NIFTY 50",
    ],
)
def test_invalid_input_is_rejected(bad: str) -> None:
    with pytest.raises(SymbolError):
        resolve(bad)


def test_non_string_is_rejected() -> None:
    with pytest.raises((SymbolError, AttributeError)):
        resolve(None)  # type: ignore[arg-type]


def test_unknown_index_name_is_not_silently_an_equity() -> None:
    # A caret symbol is not an equity ticker; it must not be resolved as NSE.
    with pytest.raises(SymbolError):
        parse_yahoo("^GSPC")
