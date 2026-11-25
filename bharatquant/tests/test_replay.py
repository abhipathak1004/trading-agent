from datetime import UTC, datetime, timedelta

import pytest

from bq.replay import ReplayBar, ReplayCursor, ReplayError


def bar(
    instant: datetime,
    *,
    instrument_id: str = "NSE:AAA",
    known_delay: timedelta = timedelta(minutes=1),
) -> ReplayBar:
    return ReplayBar(
        instrument_id=instrument_id,
        event_at=instant,
        known_at=instant + known_delay,
        open_paise=100,
        high_paise=105,
        low_paise=95,
        close_paise=102,
        volume=1000,
    )


def test_replay_cursor_only_releases_known_bars() -> None:
    first = bar(datetime(2026, 11, 23, 9, 0, tzinfo=UTC))
    second = bar(datetime(2026, 11, 23, 9, 1, tzinfo=UTC))
    cursor = ReplayCursor((first, second))

    assert cursor.advance(first.event_at) == ()
    assert cursor.advance(first.known_at) == (first,)
    assert cursor.advance(second.event_at) == ()
    assert cursor.advance(second.known_at) == (second,)


def test_replay_cursor_rejects_unordered_or_invalid_bars() -> None:
    first = bar(datetime(2026, 11, 23, 9, 1, tzinfo=UTC))
    second = bar(datetime(2026, 11, 23, 9, 0, tzinfo=UTC))
    with pytest.raises(ReplayError, match="ordered"):
        ReplayCursor((first, second))
    with pytest.raises(ReplayError, match="known before"):
        ReplayBar(
            instrument_id="NSE:AAA",
            event_at=datetime(2026, 11, 23, 9, 1, tzinfo=UTC),
            known_at=datetime(2026, 11, 23, 9, 0, tzinfo=UTC),
            open_paise=100,
            high_paise=105,
            low_paise=95,
            close_paise=102,
            volume=1000,
        )
