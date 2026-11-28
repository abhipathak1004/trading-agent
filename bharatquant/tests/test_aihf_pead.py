"""The deterministic PEAD analyst must fire on an Indian filed result.

Upstream PEAD only accepts SEC `8-K` rows as "announcements" and skips every
other source type, because a `10-Q`/`10-K` filing date is the statutory filing,
not the announcement. This app stores an NSE quarterly result as source type
`RESULT`, and its `filing_date` *is* the announcement (period end + SEBI's
45-day window). Without treating `RESULT` as an announcement, PEAD abstained on
every Indian name and the earnings-drift strategy never traded — a silent zero
that looked like a cautious model.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bq.aihf.bridge.data import BharatQuantDataClient
from bq.aihf.data_store import EarningsStore
from bq.aihf.engine.signals.pead import PEADModel
from bq.data.vendors.router import VendorResult
from bq.infra.database import migrate
from bq.settings import SettingsStore


class _NoBars:
    """PEAD reads only earnings, so a bars call must never be needed."""

    def bars(self, *_a, **_k):  # pragma: no cover
        return VendorResult(data=(), source="none", is_realtime=False)

    def quote(self, *_a, **_k):  # pragma: no cover
        raise AssertionError("PEAD must not ask for a quote")


def _client(db, store) -> BharatQuantDataClient:
    return BharatQuantDataClient(database_path=db, router=_NoBars(), clock=store.clock)


@pytest.fixture
def seeded(tmp_path):
    db = tmp_path / "bq.db"
    migrate(db, create=True)
    store = SettingsStore(db)
    EarningsStore(db, clock=store.clock).record(
        symbol="RELIANCE",
        report_period="2024-12-31",
        filing_date="2025-02-10",
        eps_actual=30.0,
        eps_estimate=27.0,
        eps_surprise=None,
        known_at=datetime(2025, 2, 10, tzinfo=UTC),
    )
    return db, store


def test_a_filed_result_fires_the_drift_signal(seeded) -> None:
    db, store = seeded
    signal = PEADModel().predict("RELIANCE", "2025-02-11", _client(db, store))
    assert signal.value == 1.0
    assert signal.metadata["eps_surprise"] == "BEAT"


def test_a_miss_fires_the_short_side(seeded) -> None:
    db, store = seeded
    # A second, later quarter that misses.
    EarningsStore(db, clock=store.clock).record(
        symbol="INFY",
        report_period="2024-12-31",
        filing_date="2025-02-10",
        eps_actual=20.0,
        eps_estimate=24.0,
        eps_surprise=None,
        known_at=datetime(2025, 2, 10, tzinfo=UTC),
    )
    signal = PEADModel().predict("INFY", "2025-02-11", _client(db, store))
    assert signal.value == -1.0
    assert signal.metadata["eps_surprise"] == "MISS"


def test_the_signal_fades_past_its_window(seeded) -> None:
    """The drift is a days-long effect: a month later the model has no view."""
    db, store = seeded
    signal = PEADModel().predict("RELIANCE", "2025-03-20", _client(db, store))
    assert signal.value == 0.0


def test_a_result_is_not_visible_before_it_was_filed(seeded) -> None:
    db, store = seeded
    signal = PEADModel().predict("RELIANCE", "2025-02-05", _client(db, store))
    assert signal.value == 0.0


def test_announcement_only_still_rejects_a_statutory_filing(tmp_path) -> None:
    """The reason for the filter survives: a 10-Q source must not fire, because
    its date is the statutory filing rather than the announcement."""
    db = tmp_path / "bq.db"
    migrate(db, create=True)
    store = SettingsStore(db)
    EarningsStore(db, clock=store.clock).record(
        symbol="XYZ",
        report_period="2024-12-31",
        filing_date="2025-02-10",
        eps_actual=30.0,
        eps_estimate=27.0,
        eps_surprise=None,
        source_type="10-Q",
        known_at=datetime(2025, 2, 10, tzinfo=UTC),
    )
    signal = PEADModel().predict("XYZ", "2025-02-11", _client(db, store))
    assert signal.value == 0.0
