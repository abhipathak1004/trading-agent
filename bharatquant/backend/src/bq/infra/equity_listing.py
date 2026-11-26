"""Bounded download of the NSE equity listing.

The news→symbol step needs company names, which the Kite instrument master does
not carry. NSE publishes a static CSV of every listed equity; this module is the
only place that fetches it, so tests never touch the network.
"""

from __future__ import annotations

import httpx

NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
MAX_LISTING_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
_USER_AGENT = "Mozilla/5.0 BharatQuant/0.1 (personal research)"


class ListingFetchError(ValueError):
    """The listing could not be fetched within the size and transport limits."""


def fetch_listing(
    url: str = NSE_EQUITY_LIST_URL,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    """Fetch the listing, refusing anything but HTTPS and over-large bodies."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ListingFetchError("Listing URL must use HTTPS.")
    if timeout <= 0:
        raise ListingFetchError("Listing timeout must be positive.")
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/csv,text/plain"},
        ) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_LISTING_BYTES:
                        raise ListingFetchError(
                            "Listing payload exceeds the size limit."
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPStatusError as exc:
        raise ListingFetchError(
            f"Listing returned HTTP {exc.response.status_code}."
        ) from exc
    except httpx.HTTPError as exc:
        raise ListingFetchError(
            f"Listing request failed: {type(exc).__name__}."
        ) from exc


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_LISTING_BYTES",
    "NSE_EQUITY_LIST_URL",
    "ListingFetchError",
    "fetch_listing",
]
