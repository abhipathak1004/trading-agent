"""Resolve an Indian-equity ticker into every vendor's spelling.

One canonical identity, several vendor formats:

    canonical   NSE:RELIANCE            (default exchange is NSE)
    Yahoo       RELIANCE.NS / RELIANCE.BO
    Kite        ("NSE", "RELIANCE")

Indices resolve too, but they are not tradeable and carry no exchange symbol:
`NIFTY 50` becomes `^NSEI` for Yahoo. A `Symbol` therefore reports
``tradeable`` so a later stage cannot accidentally size a position in an index.

This module is deliberately separate from ``bq.data.yahoo``: that module knows
how to *fetch* a Yahoo chart and validates whatever string it is handed. This one
owns the *translation* between vendor dialects, including the index table.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MAX_SYMBOL_CHARS = 24
DEFAULT_EXCHANGE = "NSE"

_YAHOO_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}

# Index names people actually type, mapped to Yahoo's caret symbols. Keys are
# compared after upper-casing and collapsing internal whitespace.
_YAHOO_INDEX = {
    "NIFTY 50": "^NSEI",
    "NIFTY50": "^NSEI",
    "NIFTY": "^NSEI",
    "NIFTY BANK": "^NSEBANK",
    "NIFTYBANK": "^NSEBANK",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
    "INDIA VIX": "^INDIAVIX",
    "INDIAVIX": "^INDIAVIX",
}

# The reverse map lets a canonical look like "INDEX:NIFTY 50" and still print a
# readable name.
_INDEX_NAME = {
    "^NSEI": "NIFTY 50",
    "^NSEBANK": "NIFTY BANK",
    "^BSESN": "SENSEX",
    "^INDIAVIX": "INDIA VIX",
}


class SymbolError(ValueError):
    """Raised when a ticker cannot be resolved. Never falls back silently."""


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"
    INDEX = "INDEX"


@dataclass(frozen=True, slots=True)
class Symbol:
    """A resolved instrument identity.

    ``tradingsymbol`` is the bare ticker without a vendor suffix. For an index
    it holds the display name (``NIFTY 50``) and ``exchange`` is ``INDEX``.
    """

    exchange: Exchange
    tradingsymbol: str

    def __post_init__(self) -> None:
        if not isinstance(self.exchange, Exchange):
            raise SymbolError("Exchange must be an Exchange value.")
        if not self.tradingsymbol:
            raise SymbolError("Trading symbol is required.")

    @property
    def tradeable(self) -> bool:
        return self.exchange is not Exchange.INDEX

    @property
    def canonical(self) -> str:
        return f"{self.exchange.value}:{self.tradingsymbol}"


def _checked(text: str) -> str:
    """Validate one ticker or index name before it reaches a vendor URL."""
    if not isinstance(text, str):
        raise SymbolError("Symbol must be text.")
    value = text.strip()
    if not value:
        raise SymbolError("Symbol is empty.")
    if len(value) > MAX_SYMBOL_CHARS:
        raise SymbolError(f"Symbol must be at most {MAX_SYMBOL_CHARS} characters.")
    if any(char in value for char in " \t\r\n/\\?#&="):
        raise SymbolError("Symbol must not contain whitespace or URL characters.")
    return value.upper()


def _index_name(normalised: str) -> str | None:
    collapsed = " ".join(normalised.split())
    return _INDEX_NAME.get(_YAHOO_INDEX.get(collapsed, ""))


def resolve(text: str) -> Symbol:
    """Parse ``RELIANCE``, ``NSE:RELIANCE``, ``RELIANCE.NS`` or an index name."""
    raw = text.strip() if isinstance(text, str) else ""
    if not raw:
        raise SymbolError("Symbol is empty.")

    # Index names contain spaces, so check them before the generic validator.
    collapsed = " ".join(raw.upper().split())
    if collapsed in _YAHOO_INDEX:
        name = _index_name(collapsed)
        assert name is not None  # the two maps are defined together
        return Symbol(Exchange.INDEX, name)

    if ":" in raw:
        exchange_part, _, ticker_part = raw.partition(":")
        try:
            exchange = Exchange(exchange_part.strip().upper())
        except ValueError as exc:
            raise SymbolError(f"Unknown exchange: {exchange_part.strip()}.") from exc
        if exchange is Exchange.INDEX:
            raise SymbolError("Indices are resolved by name, not by INDEX: prefix.")
        return Symbol(exchange, _checked(ticker_part))

    # Yahoo-suffixed input is accepted so a user can paste a symbol they already
    # have; the suffix is stripped and folded back into the exchange.
    upper = raw.upper()
    for exchange_name, suffix in _YAHOO_SUFFIX.items():
        if upper.endswith(suffix):
            return Symbol(Exchange(exchange_name), _checked(upper[: -len(suffix)]))

    return Symbol(Exchange(DEFAULT_EXCHANGE), _checked(raw))


def to_yahoo(symbol: Symbol) -> str:
    """Yahoo spelling: ``.NS``/``.BO`` suffix, or a caret index symbol."""
    if symbol.exchange is Exchange.INDEX:
        caret = _YAHOO_INDEX.get(symbol.tradingsymbol)
        if caret is None:
            raise SymbolError(f"No Yahoo mapping for index {symbol.tradingsymbol}.")
        return caret
    return f"{symbol.tradingsymbol}{_YAHOO_SUFFIX[symbol.exchange.value]}"


def to_kite(symbol: Symbol) -> tuple[str, str]:
    """Kite spelling: ``(exchange, tradingsymbol)``, as Kite's own routes use."""
    if not symbol.tradeable:
        raise SymbolError("Indices have no Kite trading symbol.")
    return symbol.exchange.value, symbol.tradingsymbol


def parse_yahoo(yahoo_symbol: str) -> Symbol:
    """Inverse of :func:`to_yahoo`, for interpreting a vendor response."""
    if not isinstance(yahoo_symbol, str) or not yahoo_symbol:
        raise SymbolError("Symbol is empty.")
    value = yahoo_symbol.strip().upper()
    if value in _INDEX_NAME:
        return Symbol(Exchange.INDEX, _INDEX_NAME[value])
    for exchange_name, suffix in _YAHOO_SUFFIX.items():
        if value.endswith(suffix):
            return Symbol(Exchange(exchange_name), _checked(value[: -len(suffix)]))
    raise SymbolError("Yahoo symbol has no NSE, BSE or index mapping.")


__all__ = [
    "DEFAULT_EXCHANGE",
    "Exchange",
    "Symbol",
    "SymbolError",
    "parse_yahoo",
    "resolve",
    "to_kite",
    "to_yahoo",
]
