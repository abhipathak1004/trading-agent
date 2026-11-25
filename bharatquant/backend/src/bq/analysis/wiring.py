"""Assemble the market-data vendor router from configuration.

One place builds the chain so the API and the CLI cannot disagree about which
vendor serves which domain. Daily bars route to Yahoo first — its long history is
free and its parser is the stricter one. Kite is inserted only when the owner has
enabled it **and** credentials exist, so a disabled Kite is simply absent from the
chain rather than failing every request.

Kite is read-only (ADR-013): this module calls `instruments` and nothing else
during setup. Quote and candle calls happen in the vendor itself.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from bq.data.broker_credentials import BrokerCredentialStore
from bq.data.instrument_master import InstrumentMasterStore
from bq.data.vendors import DataDomain, VendorRouter
from bq.data.vendors.kite_vendor import KiteVendor
from bq.data.vendors.yahoo_vendor import YahooVendor


def _instrument_tokens(
    database_path: Path, exchange: str
) -> dict[tuple[str, str], int]:
    """Read an exchange's tokens from the most recent cached dump."""
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute(
            "SELECT tradingsymbol, instrument_token FROM instrument_master "
            "WHERE exchange = ? AND as_of_date = ("
            "SELECT MAX(as_of_date) FROM instrument_master WHERE exchange = ?)",
            (exchange, exchange),
        ).fetchall()
    return {(exchange, str(symbol)): int(token) for symbol, token in rows}


def build_analysis_router(database_path: Path, config: object) -> VendorRouter:
    """Build the vendor router for this configuration and database."""
    router = VendorRouter()
    yahoo = YahooVendor()
    router.register_vendor(DataDomain.BARS_DAILY, yahoo, priority=0)
    router.register_vendor(DataDomain.QUOTES_LIVE, yahoo)
    router.register_vendor(DataDomain.INDICES, yahoo)

    enabled = bool(getattr(config, "kite_enabled", False))
    credentials = BrokerCredentialStore(database_path).credentials()
    if not (enabled and credentials is not None):
        return router

    import kiteconnect

    api_key, _secret, access_token = credentials
    client = kiteconnect.KiteConnect(api_key=api_key, access_token=access_token)
    tokens: dict[tuple[str, str], int] = {}
    try:
        master = InstrumentMasterStore(database_path)
        for exchange in ("NSE", "BSE"):
            if master.cached_date(exchange) is None:
                master.refresh(exchange, client.instruments(exchange))
            tokens.update(_instrument_tokens(database_path, exchange))
    except Exception:
        # A credential or master failure must not break read-only analysis: the
        # chain keeps Yahoo and the caller simply gets delayed data.
        return router

    kite = KiteVendor(client, instrument_tokens=tokens)
    router.register_vendor(DataDomain.QUOTES_LIVE, kite, priority=0)
    router.register_vendor(DataDomain.BARS_INTRADAY, kite, priority=0)
    router.register_vendor(DataDomain.BARS_DAILY, kite)
    router.register_vendor(DataDomain.INDICES, kite)
    return router


__all__ = ["build_analysis_router"]
