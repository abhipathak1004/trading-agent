"""Tests for the market and web tools, using mocked transports."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from bq.tools.base import ToolValidationError
from bq.tools.market import (
    StockQuoteTool,
    WebFetchTool,
    _strip_html,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def _chart(symbol: str = "RELIANCE.NS") -> bytes:
    closes = [100.0, 102.0, 105.0]
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": symbol, "currency": "INR"},
                    "timestamp": [1794368700, 1794455100, 1794541500],
                    "indicators": {
                        "quote": [
                            {
                                "open": [c - 1 for c in closes],
                                "high": [c + 2 for c in closes],
                                "low": [c - 2 for c in closes],
                                "close": closes,
                                "volume": [1000, 1100, 1200],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }
    return json.dumps(body).encode("utf-8")


def test_stock_quote_summarises_latest_bar() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_chart())

    tool = StockQuoteTool(FrozenClock(), transport=httpx.MockTransport(handler))
    result = tool.run({"symbol": "RELIANCE.NS", "range": "5d"})
    assert "RELIANCE.NS" in result.message
    assert "close 105.0" in result.message
    assert "+3.00" in result.message  # 102 -> 105


def test_stock_quote_handles_bad_symbol_gracefully() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {"chart": {"result": None, "error": {"code": "x"}}}
            ).encode(),
        )

    tool = StockQuoteTool(FrozenClock(), transport=httpx.MockTransport(handler))
    result = tool.run({"symbol": "NOPE.NS"})
    assert "failed" in result.message


def test_stock_quote_rejects_missing_symbol() -> None:
    tool = StockQuoteTool(FrozenClock())
    with pytest.raises(ToolValidationError):
        tool.run({})


def test_web_fetch_extracts_readable_text() -> None:
    html = b"""<html><head><style>.x{color:red}</style><script>bad()</script></head>
    <body><h1>Headline</h1><p>First para.</p><p>Second para.</p></body></html>"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=html, headers={"content-type": "text/html; charset=utf-8"}
        )

    tool = WebFetchTool(FrozenClock(), transport=httpx.MockTransport(handler))
    result = tool.run({"url": "https://example.com/article", "max_chars": 5000})
    assert "Headline" in result.message
    assert "First para." in result.message
    assert "bad()" not in result.message  # script removed
    assert "color:red" not in result.message  # style removed


def test_web_fetch_refuses_non_https() -> None:
    tool = WebFetchTool(FrozenClock())
    result = tool.run({"url": "http://insecure.example/x"})
    assert "refused" in result.message


def test_web_fetch_reports_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    tool = WebFetchTool(FrozenClock(), transport=httpx.MockTransport(handler))
    result = tool.run({"url": "https://example.com/missing"})
    assert "HTTP 404" in result.message


def test_strip_html_collapses_and_unwraps() -> None:
    text = _strip_html("<div>A &amp; B</div><br/><span>C</span>")
    assert "A & B" in text
    assert "C" in text
    assert "<" not in text
