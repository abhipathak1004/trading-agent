"""Typed market-data vendor failures.

The distinction that matters to a caller:

* :class:`VendorUnavailableError` — this vendor could not answer *right now*
  (network, auth token expired, rate limit, 5xx). Try the next vendor.
* :class:`NoMarketDataError` — the vendor answered and there is nothing to give
  (unknown symbol, delisted, empty series). Trying the next vendor is still
  reasonable, but if every vendor says this the answer is "no data", not
  "everything is broken".

Keeping these apart is what stops a fallback chain from looking successful when
it actually has no data, and stops a genuine outage being reported as a missing
symbol. Both inherit ``ValueError`` so an existing ``except ValueError`` at a
boundary keeps working.
"""


class VendorError(ValueError):
    """Base class for a market-data vendor failure."""


class VendorUnavailableError(VendorError):
    """The vendor could not serve the request; another vendor may succeed.

    ``reason`` is a short machine-readable tag (``kite_auth_expired``,
    ``rate_limited``, ``transport``) so callers and logs can act on it without
    parsing a message.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("A vendor failure reason is required.")
        self.reason = reason
        super().__init__(message or reason)


class NoMarketDataError(VendorError):
    """The vendor answered, but has no data for this symbol or window."""

    def __init__(self, symbol: str, message: str | None = None) -> None:
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("A symbol is required.")
        self.symbol = symbol
        super().__init__(message or f"No market data for {symbol}.")


__all__ = ["NoMarketDataError", "VendorError", "VendorUnavailableError"]
