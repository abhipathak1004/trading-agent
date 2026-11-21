"""Trading-calendar resolution.

NSE holidays are never hardcoded. A day counts as a trading day only if the
data source actually has data for it, so a missing file *is* the holiday.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from pinak.core.errors import DataUnavailableError


def resolve_trading_days(
    fetch: Callable[[date], Any],
    end: date,
    count: int,
    minimum: int | None = None,
    max_attempts: int | None = None,
) -> list[date]:
    """Walk back from ``end`` collecting ``count`` days that have data."""
    days: list[date] = []
    cursor = end
    attempts = 0
    limit = max_attempts or count * 4
    last_error = "none"

    while len(days) < count and attempts < limit:
        attempts += 1
        if cursor.weekday() < 5:
            try:
                fetch(cursor)
            except DataUnavailableError as exc:
                last_error = str(exc)
            else:
                days.append(cursor)
        cursor -= timedelta(days=1)

    required = minimum if minimum is not None else 1
    if len(days) < required:
        raise DataUnavailableError(
            f"only {len(days)} trading days found before {end}; need {required} "
            f"(last error: {last_error})"
        )
    return sorted(days)
