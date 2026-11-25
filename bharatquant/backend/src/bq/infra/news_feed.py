"""Bounded HTTP fetching for the news agent.

Kept separate from parsing and storage so tests never touch the network and so
the one place that talks to the outside world is small and auditable.
"""

import httpx

from bq.news import MAX_FEED_BYTES, PULSE_FEED_URL, NewsError

DEFAULT_TIMEOUT_SECONDS = 15.0
_USER_AGENT = "BharatQuant/0.1 (personal research; +https://pulse.zerodha.com/)"


def fetch_feed(
    url: str = PULSE_FEED_URL,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    """Fetch a news feed, refusing anything but HTTPS and over-large bodies."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise NewsError("Feed URL must use HTTPS.")
    if timeout <= 0:
        raise NewsError("Feed timeout must be positive.")
    limit = MAX_FEED_BYTES
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/rss+xml, application/xml;q=0.9",
            },
        ) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise NewsError("Feed payload exceeds the size limit.")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPStatusError as exc:
        raise NewsError(f"Feed returned HTTP {exc.response.status_code}.") from exc
    except httpx.HTTPError as exc:
        raise NewsError(f"Feed request failed: {type(exc).__name__}.") from exc
