from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from bq.core import CurrencyPaise
from bq.infra.database import migrate
from bq.universe import UniverseError, UniverseStore


class FrozenClock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


def test_published_universe_filters_and_is_point_in_time(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    clock = FrozenClock()
    store = UniverseStore(path, clock)

    version = store.publish(
        universe_id="nse-main",
        effective_date=date(2026, 11, 20),
        minimum_market_cap_paise=CurrencyPaise(5_000),
        source_id="owner-review",
        source_url="https://example.test/source",
        market_caps=(
            ("NSE:AAA", CurrencyPaise(8_000)),
            ("NSE:BBB", CurrencyPaise(4_000)),
        ),
    )
    assert version.version == 1
    assert version.instrument_ids == ("NSE:AAA",)

    with pytest.raises(UniverseError, match="unavailable"):
        store.get(
            "nse-main",
            effective_date=date(2026, 11, 20),
            as_of=datetime(2026, 11, 23, 11, 59, tzinfo=UTC),
        )


def test_universe_duplicate_instrument_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = UniverseStore(path)

    with pytest.raises(UniverseError, match="unique"):
        store.publish(
            universe_id="nse-main",
            effective_date=date(2026, 11, 20),
            minimum_market_cap_paise=CurrencyPaise(5_000),
            source_id="owner-review",
            source_url=None,
            market_caps=(
                ("NSE:AAA", CurrencyPaise(8_000)),
                ("NSE:AAA", CurrencyPaise(9_000)),
            ),
        )
