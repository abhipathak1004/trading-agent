"""Assemble the engine's data and model clients from this app's configuration.

One place builds them so the routes, the scheduler job and a test all agree: the
engine's `DataClient` is this app's vendor router plus its point-in-time stores,
and a persona's `LLMClient` is this app's model router. Nothing here names a
vendor or a model; both come from `SettingsStore` and the existing analysis
wiring.
"""

from __future__ import annotations

from bq.aihf.bridge.data import BharatQuantDataClient
from bq.analysis.wiring import build_analysis_router
from bq.settings import SettingsStore


def build_data_client(settings: SettingsStore) -> BharatQuantDataClient:
    """The engine's `DataClient` for this app's database and vendors."""
    router = build_analysis_router(settings.path, settings.load())
    return BharatQuantDataClient(
        database_path=settings.path, router=router, clock=settings.clock
    )


__all__ = ["build_data_client"]
