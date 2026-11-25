"""Per-domain vendor routing with explicit fallback.

The chain for each :class:`DataDomain` is an ordered list of vendors. A request
walks the chain until a vendor answers, then the result carries that vendor's
identity and real-time flag back to the caller. Adding a vendor is a public
:meth:`VendorRouter.register_vendor` call — no core file is edited, which is the
extension point the migration asked for.

Two deliberate choices:

* **No silent substitution.** A result always records which vendor produced it
  and whether the data is real-time. Downstream code that needs live prices
  checks ``is_realtime``; it never assumes.
* **Failures are collected, not swallowed.** If every vendor fails, the raised
  error says which vendors were tried and why each failed. A caller debugging a
  missing price gets an explanation instead of an empty list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, TypeVar

from bq.data.symbols import Symbol
from bq.data.vendors.base import (
    Bar,
    BarInterval,
    DataDomain,
    MarketDataVendor,
    Quote,
)
from bq.data.vendors.errors import (
    NoMarketDataError,
    VendorError,
    VendorUnavailableError,
)

T = TypeVar("T")


class RoutingError(VendorError):
    """Every vendor in the chain failed.

    ``failures`` preserves order so the message reads like a diagnosis:
    ``kite: kite_auth_expired; yahoo: rate_limited``.
    """

    def __init__(self, domain: DataDomain, failures: tuple[tuple[str, str], ...]):
        self.domain = domain
        self.failures = failures
        detail = (
            "; ".join(f"{name}: {reason}" for name, reason in failures) or "no vendors"
        )
        super().__init__(f"No vendor could serve {domain.value} ({detail}).")


class VendorNotRegisteredError(RoutingError):
    """The chain for this domain is empty."""


@dataclass(frozen=True, slots=True)
class VendorResult[T]:
    """Data plus the provenance needed to describe where it came from."""

    data: T
    source: str
    is_realtime: bool


def _reason(exc: Exception) -> str:
    if isinstance(exc, VendorUnavailableError):
        return exc.reason
    if isinstance(exc, NoMarketDataError):
        return "no_data"
    return type(exc).__name__


class VendorRouter:
    """Holds ordered per-domain chains and routes requests through them."""

    def __init__(self) -> None:
        self._chains: dict[DataDomain, list[MarketDataVendor]] = {}

    # -- registration -----------------------------------------------------

    def register_vendor(
        self,
        domain: DataDomain,
        vendor: MarketDataVendor,
        *,
        priority: int | None = None,
    ) -> None:
        """Add ``vendor`` to ``domain``'s chain.

        ``priority`` is a position: ``0`` is tried first. ``None`` appends. A
        vendor already in the chain is moved rather than duplicated, so calling
        this twice is idempotent.
        """
        if not isinstance(domain, DataDomain):
            raise TypeError("A DataDomain is required.")
        if not isinstance(vendor, MarketDataVendor):
            raise TypeError("A MarketDataVendor is required.")
        if domain not in vendor.capabilities:
            raise VendorError(f"Vendor '{vendor.name}' does not claim {domain.value}.")
        chain = self._chains.setdefault(domain, [])
        chain[:] = [existing for existing in chain if existing.name != vendor.name]
        if priority is None:
            chain.append(vendor)
        else:
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise TypeError("priority must be an integer position.")
            if not 0 <= priority <= len(chain):
                raise ValueError("priority is outside the chain.")
            chain.insert(priority, vendor)

    def chain(self, domain: DataDomain) -> tuple[MarketDataVendor, ...]:
        """The vendors that would be tried, in order. For diagnostics and UI."""
        return tuple(self._chains.get(domain, ()))

    # -- routing ----------------------------------------------------------

    def route(
        self,
        domain: DataDomain,
        call: Callable[[MarketDataVendor], T],
        *,
        symbol: Symbol | None = None,
    ) -> VendorResult[T]:
        """Try each vendor in order and return the first answer with its source."""
        chain = self._chains.get(domain, [])
        if not chain:
            raise VendorNotRegisteredError(domain, ())
        failures: list[tuple[str, str]] = []
        for vendor in chain:
            try:
                data = call(vendor)
            except VendorError as exc:
                failures.append((vendor.name, _reason(exc)))
                continue
            except Exception as exc:  # a vendor bug must not look like an outage
                failures.append((vendor.name, type(exc).__name__))
                continue
            return VendorResult(
                data=data, source=vendor.name, is_realtime=vendor.is_realtime
            )
        # No vendor answered. If every one said "no data", that is the accurate
        # report, not a generic outage.
        if (
            symbol is not None
            and failures
            and all(reason == "no_data" for _, reason in failures)
        ):
            raise NoMarketDataError(symbol.canonical)
        raise RoutingError(domain, tuple(failures))

    # -- typed helpers ----------------------------------------------------

    def quote(self, symbol: Symbol) -> VendorResult[Quote]:
        return self.route(
            DataDomain.QUOTES_LIVE, lambda vendor: vendor.quote(symbol), symbol=symbol
        )

    def bars(
        self,
        symbol: Symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
        domain: DataDomain | None = None,
    ) -> VendorResult[tuple[Bar, ...]]:
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError("Bar request bounds must be timezone-aware.")
        if end < start:
            raise ValueError("Bar request end is before its start.")
        target = domain or (
            DataDomain.BARS_DAILY
            if interval is BarInterval.DAY_1
            else DataDomain.BARS_INTRADAY
        )
        return self.route(
            target,
            lambda vendor: vendor.bars(symbol, interval=interval, start=start, end=end),
            symbol=symbol,
        )


__all__ = [
    "RoutingError",
    "VendorNotRegisteredError",
    "VendorResult",
    "VendorRouter",
]
