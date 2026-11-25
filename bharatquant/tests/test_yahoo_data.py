import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bq.data.records import DataRecordStore, RecordKind
from bq.data.yahoo import (
    YahooDataClient,
    YahooError,
    import_bars,
    parse_chart,
)
from bq.infra.database import migrate

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def _chart(
    *,
    symbol: str = "RELIANCE.NS",
    currency: str = "INR",
    closes: list[object] | None = None,
) -> bytes:
    closes = closes if closes is not None else [100.0, 101.0, 102.0]
    stamps = [1794368700, 1794455100, 1794541500][: len(closes)]
    series = {
        "open": [c - 1 for c in closes if c is not None],
    }
    # Build OHLC consistent with close values; preserve None positions.
    opens = [None if c is None else c - 1 for c in closes]
    highs = [None if c is None else c + 2 for c in closes]
    lows = [None if c is None else c - 2 for c in closes]
    volumes = [1000 if c is not None else None for c in closes]
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": symbol, "currency": currency},
                    "timestamp": stamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": volumes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }
    del series
    return json.dumps(body).encode("utf-8")


def test_parse_chart_returns_complete_bars() -> None:
    symbol, currency, bars = parse_chart(_chart(), received_at=NOW)
    assert symbol == "RELIANCE.NS"
    assert currency == "INR"
    assert len(bars) == 3
    assert str(bars[0].close) == "100.0"


def test_parse_chart_drops_incomplete_rows() -> None:
    payload = _chart(closes=[100.0, None, 102.0])
    _symbol, _currency, bars = parse_chart(payload, received_at=NOW)
    # The null close row is dropped.
    assert len(bars) == 2


def test_parse_chart_rejects_error_and_empty() -> None:
    with pytest.raises(YahooError, match="error"):
        parse_chart(
            json.dumps({"chart": {"result": None, "error": {"code": "x"}}}).encode(),
            received_at=NOW,
        )
    with pytest.raises(YahooError):
        parse_chart(b"not json", received_at=NOW)


def test_import_bars_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = DataRecordStore(path, clock)
    _symbol, currency, bars = parse_chart(_chart(), received_at=NOW)

    first = import_bars(
        store, symbol="RELIANCE.NS", bars=bars, currency=currency, import_key="k1"
    )
    assert len(first.record_ids) == 3
    second = import_bars(
        store, symbol="RELIANCE.NS", bars=bars, currency=currency, import_key="k1"
    )
    assert second.import_id == first.import_id
    assert second.record_ids == first.record_ids


def test_download_uses_injected_transport(tmp_path: Path) -> None:
    import httpx

    path = tmp_path / "bq.db"
    migrate(path, create=True)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_chart())

    client = YahooDataClient(
        path, clock=FrozenClock(), transport=httpx.MockTransport(handler)
    )
    result = client.download("RELIANCE.NS", range_="1mo", interval="1d")
    assert result.record_count == 3
    assert result.symbol == "RELIANCE.NS"

    store = DataRecordStore(path, FrozenClock())
    _symbol, _currency, bars = parse_chart(_chart(), received_at=NOW)
    key = f"yahoo.finance/RELIANCE.NS/{bars[0].trading_date}/UNADJUSTED"
    record = store.record_for(RecordKind.EOD_BAR, key, as_of=NOW)
    assert record.source_id == "yahoo.finance"
    assert record.payload["close"] == "100.0"


def test_download_rejects_bad_symbol(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    client = YahooDataClient(path, clock=FrozenClock())
    with pytest.raises(YahooError):
        client.download("../etc/passwd")


def test_data_download_endpoint(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    import httpx as _httpx

    from bq.api import ALLOWED_ORIGIN, create_app
    from bq.auth import OwnerAuth

    path = tmp_path / "bq.db"
    migrate(path, create=True)
    auth = OwnerAuth(path)

    def handler(_request: _httpx.Request) -> _httpx.Response:
        return _httpx.Response(200, content=_chart())

    import bq.api as api_module

    original = api_module.YahooDataClient

    def factory(db_path, **_kwargs):  # type: ignore[no-untyped-def]
        return original(
            db_path, clock=FrozenClock(), transport=_httpx.MockTransport(handler)
        )

    monkeypatch.setattr(api_module, "YahooDataClient", factory)
    app = create_app(database_path=path)
    password = "synthetic owner password"

    async def check() -> None:
        async with _httpx.AsyncClient(
            transport=_httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            assert (
                await client.post(
                    "/api/v1/data/download", json={"symbol": "RELIANCE.NS"}
                )
            ).status_code == 401
            auth.setup(password)
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": ALLOWED_ORIGIN},
                json={"password": password},
            )
            csrf = login.json()["csrf_token"]
            ok = await client.post(
                "/api/v1/data/download",
                headers={"Origin": ALLOWED_ORIGIN, "X-CSRF-Token": csrf},
                json={"symbol": "RELIANCE.NS", "range": "1mo", "interval": "1d"},
            )
            assert ok.status_code == 200
            body = ok.json()
            assert body["symbol"] == "RELIANCE.NS"
            assert body["record_count"] == 3

    asyncio.run(check())
