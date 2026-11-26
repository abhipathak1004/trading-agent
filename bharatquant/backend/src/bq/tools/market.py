"""Tools for market data and web research.

These give the research agent the two capabilities it was missing: live price
data and the ability to read a web page. Both are HTTPS-only and bounded:

* ``stock_quote`` fetches recent daily bars from Yahoo and summarises the latest
  close, change and range. It does not persist — persisting is the separate
  ``stock_download`` tool / ``data download`` CLI, which writes point-in-time
  records.
* ``stock_download`` fetches and persists bars through the existing PIT store.
* ``web_fetch`` retrieves a single HTTPS page and reduces it to bounded plain
  text. It refuses non-HTTPS, oversized bodies, and — importantly — strips
  scripts/styles so the model sees readable content, not markup.
"""

import re
from collections.abc import Mapping
from datetime import UTC
from pathlib import Path

import httpx

from bq.core import Clock, SystemClock
from bq.data.yahoo import (
    DEFAULT_TIMEOUT_SECONDS as YAHOO_TIMEOUT,
)
from bq.data.yahoo import (
    YahooDataClient,
    YahooError,
    fetch_chart,
    parse_chart,
)
from bq.tools.base import (
    Tool,
    ToolArgument,
    ToolResult,
    ToolSpec,
    bounded_text,
    decimal_int,
)

MAX_FETCH_BYTES = 2 * 1024 * 1024
MAX_PAGE_CHARS = 12_000
MAX_URL_CHARS = 2_048
WEB_TIMEOUT_SECONDS = 20.0
_USER_AGENT = "Mozilla/5.0 BharatQuant/0.1 (personal research)"
_VALID_RANGES = {"5d", "1mo", "3mo", "6mo", "1y"}


class _SymbolToolsBase:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()


class StockQuoteTool(Tool, _SymbolToolsBase):
    """Latest price snapshot for one symbol, from recent daily bars."""

    spec = ToolSpec(
        name="stock_quote",
        summary=(
            "Get a recent price snapshot for an Indian equity symbol, e.g. "
            "RELIANCE.NS or TCS.NS: latest close, change, day range and volume."
        ),
        arguments=(
            ToolArgument("symbol", "Yahoo symbol, e.g. RELIANCE.NS.", required=True),
            ToolArgument(
                "range",
                "Lookback window: 5d, 1mo, 3mo, 6mo or 1y. Default 1mo.",
                required=False,
            ),
        ),
        mutating=False,
    )

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        _SymbolToolsBase.__init__(self, clock)
        self.transport = transport

    def execute(self, args: Mapping[str, object]) -> ToolResult:
        symbol = bounded_text(args.get("symbol"), name="symbol", maximum=24)
        range_ = "1mo"
        if "range" in args:
            range_ = bounded_text(args.get("range"), name="range", maximum=8)
        if range_ not in _VALID_RANGES:
            range_ = "1mo"
        try:
            raw = fetch_chart(
                symbol,
                range_=range_,
                interval="1d",
                timeout=YAHOO_TIMEOUT,
                transport=self.transport,
            )
            resolved, currency, bars = parse_chart(
                raw, received_at=self.clock.now().astimezone(UTC)
            )
        except YahooError as exc:
            return ToolResult(message=f"stock_quote failed for {symbol}: {exc}")
        latest = bars[-1]
        previous = bars[-2] if len(bars) > 1 else latest
        change = latest.close - previous.close
        pct = (change / previous.close * 100) if previous.close else 0
        closes = [bar.close for bar in bars]
        message = (
            f"{resolved} ({currency}) — {len(bars)} daily bars over {range_}.\n"
            f"  latest {latest.trading_date}: close {latest.close} "
            f"(open {latest.open}, high {latest.high}, low {latest.low}, "
            f"vol {latest.volume})\n"
            f"  change vs prior close: {change:+.2f} ({pct:+.2f}%)\n"
            f"  window close range: {min(closes)} – {max(closes)}\n"
            f"  source: Yahoo Finance (unassessed)"
        )
        return ToolResult(message=message)


class StockDownloadTool(Tool, _SymbolToolsBase):
    """Fetch and persist daily bars into the point-in-time store."""

    spec = ToolSpec(
        name="stock_download",
        summary=(
            "Download daily bars for a symbol and store them as unassessed "
            "point-in-time records. Use when the user wants the data kept."
        ),
        arguments=(
            ToolArgument("symbol", "Yahoo symbol, e.g. RELIANCE.NS.", required=True),
            ToolArgument(
                "range", "Lookback window: 5d, 1mo, 3mo, 6mo or 1y.", required=False
            ),
        ),
        mutating=True,
    )

    def __init__(
        self,
        path: Path,
        clock: Clock | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        _SymbolToolsBase.__init__(self, clock)
        self.path = path
        self.transport = transport

    def execute(self, args: Mapping[str, object]) -> ToolResult:
        symbol = bounded_text(args.get("symbol"), name="symbol", maximum=24)
        range_ = "1mo"
        if "range" in args:
            range_ = bounded_text(args.get("range"), name="range", maximum=8)
        if range_ not in _VALID_RANGES:
            range_ = "1mo"
        try:
            result = YahooDataClient(
                self.path, clock=self.clock, transport=self.transport
            ).download(symbol, range_=range_, interval="1d")
        except YahooError as exc:
            return ToolResult(message=f"stock_download failed for {symbol}: {exc}")
        return ToolResult(
            message=(
                f"Stored {result.record_count} daily bars for {result.symbol} "
                f"(range {range_}) as unassessed point-in-time records. "
                f"Import {result.import_id}."
            )
        )


def _strip_html(html: str) -> str:
    """Reduce HTML to bounded readable text without an HTML parser dependency."""
    # Drop script/style/noscript/svg blocks entirely.
    text = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
    # Drop comments.
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    # Treat block-level tags as line breaks.
    text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", text)
    # Remove remaining tags.
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    # Unescape the handful of entities that matter for readability.
    for entity, char in (
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&nbsp;", " "),
    ):
        text = text.replace(entity, char)
    # Collapse whitespace.
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class WebFetchTool(Tool, _SymbolToolsBase):
    """Fetch one HTTPS page and return bounded readable text."""

    spec = ToolSpec(
        name="web_fetch",
        summary=(
            "Fetch a single HTTPS web page and return its readable text, "
            "truncated. Use for an article the user linked or a page news_search "
            "pointed at. Non-HTTPS URLs are refused."
        ),
        arguments=(
            ToolArgument("url", "Absolute HTTPS URL to read.", required=True),
            ToolArgument(
                "max_chars",
                "Maximum characters to return (500 to 12000). Default 6000.",
                required=False,
                kind=int,
            ),
        ),
        mutating=False,
    )

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        _SymbolToolsBase.__init__(self, clock)
        self.transport = transport

    def execute(self, args: Mapping[str, object]) -> ToolResult:
        url = bounded_text(args.get("url"), name="url", maximum=MAX_URL_CHARS)
        if not url.startswith("https://") or " " in url:
            return ToolResult(message="web_fetch refused: URL must be HTTPS.")
        max_chars = 6000
        if "max_chars" in args:
            max_chars = decimal_int(
                args.get("max_chars"),
                name="max_chars",
                minimum=500,
                maximum=MAX_PAGE_CHARS,
            )
        try:
            with httpx.Client(
                timeout=WEB_TIMEOUT_SECONDS,
                transport=self.transport,
                follow_redirects=True,
            ) as client:
                with client.stream(
                    "GET", url, headers={"User-Agent": _USER_AGENT}
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_FETCH_BYTES:
                            return ToolResult(
                                message="web_fetch refused: page exceeded size limit."
                            )
                        chunks.append(chunk)
            body = b"".join(chunks)
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                message=f"web_fetch failed: HTTP {exc.response.status_code}."
            )
        except httpx.HTTPError as exc:
            return ToolResult(message=f"web_fetch failed: {type(exc).__name__}.")
        encoding = "utf-8"
        match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type)
        if match:
            encoding = match.group(1)
        try:
            decoded = body.decode(encoding, errors="replace")
        except LookupError:
            decoded = body.decode("utf-8", errors="replace")
        if "html" in content_type or decoded.lstrip().startswith("<"):
            text = _strip_html(decoded)
        else:
            text = decoded.strip()
        if not text:
            return ToolResult(message="web_fetch returned no readable text.")
        truncated = len(text) > max_chars
        snippet = text[:max_chars]
        note = "\n\n[truncated]" if truncated else ""
        return ToolResult(
            message=f"web_fetch {url} ({len(text)} chars):\n\n{snippet}{note}"
        )


def market_tools(
    path: Path,
    *,
    clock: Clock | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[Tool, ...]:
    """Live market + web tools for the research agent."""
    return (
        StockQuoteTool(clock, transport=transport),
        StockDownloadTool(path, clock, transport=transport),
        WebFetchTool(clock, transport=transport),
    )
