"""The market the engine runs in: NSE, on the IST clock.

Upstream is a New York, US-equity engine in two specific places, and both matter
because they decide *when* a session may be looked at rather than what the data
says:

* ``data/sessions.py`` — ``completed_through`` returns "yesterday in New York",
  ``previous_day`` and the benchmark's session grid are all NY dates;
* ``pipeline/clock.py`` — ``DailyClock``/``BarClock``/``LiveClock`` each call
  those, and the whole engine otherwise treats "a session" as an opaque string.

Rather than rewrite the vendored clock, this module supplies the same handful of
functions against IST, and the bridge clock in ``bq.aihf.bridge.clock`` delegates
to it. That keeps the engine's own logic — including its "a session is a string,
the clock decides what complete means" design — and confines the market
assumption to one small module.

Session completeness is decided two ways, in this order:

1. **Exchange calendar** (`bq.data.calendar.ExchangeCalendar`, code ``NSE``) when
   the owner has loaded a session row for that date. This is the accurate path
   and is point-in-time honest (``as_of``).
2. **Weekday + a static holiday list.** ``calendar_sessions`` is empty in a fresh
   database and the app already documents this fallback for its own scheduler
   (see ``bq/triggers.py::_session_open``), so the fund engine uses the same rule
   rather than inventing a second one.

Nothing here fetches: a market calendar is configuration in this app, loaded by
the owner. A weekday with no row is treated as a trading day, which is the
conservative choice — the engine then asks the data for the benchmark's bars and
a holiday simply has no bars, so no session is manufactured.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date as _date
from datetime import datetime, timedelta

from bq.core import (
    EXCHANGE_TIMEZONE,
    Clock,
    SystemClock,
    exchange_time,
)
from bq.data.calendar import ExchangeCalendar, SessionState, UncoveredCalendarDate

# India's exchange is NSE in this app's terms; BSE shares the trading calendar.
EXCHANGE_CODE = "NSE"

# Session close on NSE: 15:30 IST. A daily session is complete once this has
# passed, whether or not a calendar row exists for the day.
SESSION_CLOSE_HOUR = 15
SESSION_CLOSE_MINUTE = 30

# NSE trading holidays, used only when the exchange calendar has no row for a
# date. These are the fixed-date national holidays the exchange observes every
# year; movable festivals are published annually and belong in the calendar
# table, not hardcoded here (ADR-052). A date listed here is never a session.
_FIXED_MARKET_HOLIDAYS: dict[tuple[int, int], str] = {
    (1, 26): "Republic Day",
    (8, 15): "Independence Day",
    (10, 2): "Gandhi Jayanti",
    (12, 25): "Christmas",
}


class MarketError(ValueError):
    """The session grid cannot be built for the requested window."""


def exchange_now() -> datetime:
    """The current instant in IST."""
    return exchange_time(SystemClock().now())


def today() -> _date:
    """Today's IST date."""
    return exchange_now().date()


def is_trading_weekday(day: _date) -> bool:
    """A Mon–Fri that is not one of the fixed market holidays."""
    return day.weekday() < 5 and (day.month, day.day) not in _FIXED_MARKET_HOLIDAYS


def previous_trading_day(day: _date) -> _date:
    """The weekday before *day*, skipping the fixed holidays."""
    candidate = day - timedelta(days=1)
    for _ in range(30):  # a fortnight is the most any holiday cluster spans
        if is_trading_weekday(candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise MarketError(f"no trading weekday within 30 days before {day}")


def completed_through(now: datetime | None = None) -> str:
    """The last IST date whose daily session has closed.

    The engine's ``DailyClock.is_complete`` is ``session <= completed_through()``,
    so this must be "the most recent date you could possibly act on", not
    "yesterday": a 20:00 run on a trading day has today's close available, and
    returning yesterday would make every evening run a day stale. After the
    close, today counts; before it (or on a weekend/holiday), the previous
    trading day.

    Deliberately does **not** consult the calendar for the *holiday* decision:
    a movable festival with no row must not make the engine believe the market
    traded. The data does that — a holiday has no benchmark bars — but the date
    bound itself stays conservative.
    """
    moment = now.astimezone(EXCHANGE_TIMEZONE) if now is not None else exchange_now()
    day = moment.date()
    after_close = (moment.hour, moment.minute) >= (
        SESSION_CLOSE_HOUR,
        SESSION_CLOSE_MINUTE,
    )
    if after_close and is_trading_weekday(day):
        return day.isoformat()
    return previous_trading_day(day).isoformat()


def previous_day(day: str) -> str:
    """The trading date before *day*: the engine's refresh cutoff for a daily
    session. A calendar gap (a long weekend) walks back to the prior weekday."""
    return previous_trading_day(_date.fromisoformat(day[:10])).isoformat()


class SessionGate:
    """Whether a date was a real trading session, from the exchange calendar
    when it has a row and from the weekday/holiday rule otherwise.

    The engine uses this only to decide whether to ask; the data still has to
    answer with bars, so a false positive costs one empty fetch and a false
    negative cannot invent a session.
    """

    def __init__(
        self,
        database_path,  # noqa: ANN001 - Path-like; kept loose so the bridge owns the type
        clock: Clock | None = None,
    ) -> None:
        self._calendar = ExchangeCalendar(database_path, clock)

    def was_session(self, day: str, *, as_of: datetime | None = None) -> bool:
        when = as_of or SystemClock().now()
        target = _date.fromisoformat(day[:10])
        try:
            session = self._calendar.session_for(EXCHANGE_CODE, target, as_of=when)
        except (UncoveredCalendarDate, ValueError):
            return is_trading_weekday(target)
        return session.state is SessionState.OPEN


def session_gate(database_path) -> Callable[[str], bool]:  # noqa: ANN001
    """A predicate ``day -> was it a session`` bound to one database.

    Bound once per run so a backtest over 250 sessions does not open 250
    connections to answer a question that is almost always "yes, a weekday".
    """
    gate = SessionGate(database_path)
    return gate.was_session
