import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from bq.core import (
    Clock,
    SystemClock,
)
from bq.core import (
    open_connection as core_open_connection,
)
from bq.core import (
    parse_timestamp as core_parse_timestamp,
)
from bq.core import (
    utc as core_utc,
)
from bq.infra.audit import append_event


def _utc(value: datetime) -> datetime:
    try:
        return core_utc(value)
    except ValueError as exc:
        raise ValueError("Calendar timestamps must be timezone-aware.") from exc


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return core_parse_timestamp(value)


EXCHANGE_CODE = re.compile(r"[A-Z][A-Z0-9._-]{0,15}")


class SessionClass(StrEnum):
    REGULAR = "REGULAR"
    SPECIAL = "SPECIAL"
    HOLIDAY = "HOLIDAY"


class SessionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class UncoveredCalendarDate(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class CalendarSession:
    exchange_code: str
    session_date: date
    session_class: SessionClass
    state: SessionState
    opens_at: datetime | None
    closes_at: datetime | None
    source_url: str
    source_published_at: datetime | None
    known_at: datetime
    reviewed_by: str
    reviewed_at: datetime

    def __post_init__(self) -> None:
        if EXCHANGE_CODE.fullmatch(self.exchange_code) is None:
            raise ValueError("Invalid exchange code.")
        if isinstance(self.session_date, datetime) or not isinstance(
            self.session_date, date
        ):
            raise TypeError("Calendar session date must be a date.")
        if not isinstance(self.session_class, SessionClass):
            raise TypeError("Invalid calendar session class.")
        if not isinstance(self.state, SessionState):
            raise TypeError("Invalid calendar session state.")
        source = urlsplit(self.source_url)
        if (
            source.scheme != "https"
            or not source.hostname
            or source.username is not None
            or source.password is not None
        ):
            raise ValueError(
                "Calendar source must be an HTTPS URL without credentials."
            )
        if self.reviewed_by != "owner":
            raise ValueError("Calendar updates must be reviewed by the owner.")

        known_at = _utc(self.known_at)
        reviewed_at = _utc(self.reviewed_at)
        if reviewed_at < known_at:
            raise ValueError("Calendar review cannot precede when the rule was known.")
        if self.source_published_at is not None:
            if _utc(self.source_published_at) > known_at:
                raise ValueError("Calendar source cannot be known before publication.")

        if self.state is SessionState.OPEN:
            if self.opens_at is None or self.closes_at is None:
                raise ValueError("Open sessions require both time boundaries.")
            if _utc(self.closes_at) <= _utc(self.opens_at):
                raise ValueError("Session close must follow its open.")
        elif self.opens_at is not None or self.closes_at is not None:
            raise ValueError("Closed sessions cannot have time boundaries.")

        if (
            self.session_class is SessionClass.HOLIDAY
            and self.state is not SessionState.CLOSED
        ):
            raise ValueError("Holiday sessions must be closed.")


@dataclass(frozen=True, slots=True)
class CalendarSessionVersion:
    session_id: str
    recorded_at: datetime
    session: CalendarSession


@dataclass(frozen=True, slots=True)
class CalendarDayCoverage:
    session_date: date
    version: CalendarSessionVersion | None


def validate_coverage_query(
    exchange_code: str, *, start: date, end: date, as_of: datetime
) -> None:
    if (
        not isinstance(exchange_code, str)
        or EXCHANGE_CODE.fullmatch(exchange_code) is None
    ):
        raise ValueError("Invalid exchange code.")
    if any(
        isinstance(day, datetime) or not isinstance(day, date) for day in (start, end)
    ):
        raise TypeError("Calendar coverage boundaries must be dates.")
    days = (end - start).days + 1
    if not 1 <= days <= 31:
        raise ValueError("Calendar coverage must span 1 to 31 inclusive dates.")
    _timestamp(as_of)


def _decode_session_version(row: tuple[object, ...]) -> CalendarSessionVersion:
    known_at = _parse_timestamp(str(row[8]))
    reviewed_at = _parse_timestamp(str(row[10]))
    recorded_at = _parse_timestamp(str(row[12]))
    if (
        known_at is None
        or reviewed_at is None
        or recorded_at is None
        or recorded_at.utcoffset() is None
    ):
        raise sqlite3.DatabaseError("Calendar record is missing review timestamps.")
    session = CalendarSession(
        exchange_code=str(row[0]),
        session_date=date.fromisoformat(str(row[1])),
        session_class=SessionClass(str(row[2])),
        state=SessionState(str(row[3])),
        opens_at=_parse_timestamp(str(row[4]) if row[4] is not None else None),
        closes_at=_parse_timestamp(str(row[5]) if row[5] is not None else None),
        source_url=str(row[6]),
        source_published_at=_parse_timestamp(
            str(row[7]) if row[7] is not None else None
        ),
        known_at=known_at,
        reviewed_by=str(row[9]),
        reviewed_at=reviewed_at,
    )
    return CalendarSessionVersion(str(row[11]), recorded_at, session)


class ExchangeCalendar:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return core_open_connection(self.path, foreign_keys=False)

    def record(self, session: CalendarSession) -> str:
        session_id = secrets.token_urlsafe(18)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                now = _utc(self.clock.now())
                if _utc(session.reviewed_at) > now:
                    raise ValueError("Calendar review time cannot be in the future.")
                previous = connection.execute(
                    "SELECT MAX(recorded_at) FROM calendar_sessions "
                    "WHERE exchange_code = ? AND session_date = ?",
                    (session.exchange_code, session.session_date.isoformat()),
                ).fetchone()
                if previous[0] is not None and _timestamp(now) <= previous[0]:
                    raise ValueError(
                        "Calendar record time must advance for the same date."
                    )
                connection.execute(
                    "INSERT INTO calendar_sessions "
                    "(session_id, exchange_code, session_date, session_class, state, "
                    "opens_at, closes_at, source_url, source_published_at, known_at, "
                    "reviewed_by, reviewed_at, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        session.exchange_code,
                        session.session_date.isoformat(),
                        session.session_class.value,
                        session.state.value,
                        _timestamp(session.opens_at) if session.opens_at else None,
                        _timestamp(session.closes_at) if session.closes_at else None,
                        session.source_url,
                        (
                            _timestamp(session.source_published_at)
                            if session.source_published_at
                            else None
                        ),
                        _timestamp(session.known_at),
                        session.reviewed_by.strip(),
                        _timestamp(session.reviewed_at),
                        _timestamp(now),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=now,
                    actor="owner",
                    action="calendar.session.recorded",
                    object_type="calendar_session",
                    object_id=session_id,
                    payload={
                        "exchange_code": session.exchange_code,
                        "session_date": session.session_date.isoformat(),
                        "session_class": session.session_class.value,
                        "state": session.state.value,
                    },
                )
        return session_id

    def session_for(
        self, exchange_code: str, session_date: date, *, as_of: datetime
    ) -> CalendarSession:
        if (
            not isinstance(exchange_code, str)
            or EXCHANGE_CODE.fullmatch(exchange_code) is None
        ):
            raise ValueError("Invalid exchange code.")
        if isinstance(session_date, datetime) or not isinstance(session_date, date):
            raise TypeError("Calendar session date must be a date.")
        as_of_text = _timestamp(as_of)
        with closing(self._connect()) as connection:
            version = self._session_version(
                connection, exchange_code, session_date, as_of_text
            )
        if version is None:
            raise UncoveredCalendarDate(
                f"No reviewed calendar entry for {exchange_code} on {session_date} "
                f"as of {as_of_text}."
            )
        return version.session

    def coverage(
        self, exchange_code: str, *, start: date, end: date, as_of: datetime
    ) -> tuple[CalendarDayCoverage, ...]:
        validate_coverage_query(exchange_code, start=start, end=end, as_of=as_of)
        days = (end - start).days + 1
        as_of_text = _timestamp(as_of)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            dates = (start + timedelta(days=index) for index in range(days))
            return tuple(
                CalendarDayCoverage(
                    day,
                    self._session_version(connection, exchange_code, day, as_of_text),
                )
                for day in dates
            )

    @staticmethod
    def _session_version(
        connection: sqlite3.Connection,
        exchange_code: str,
        session_date: date,
        as_of_text: str,
    ) -> CalendarSessionVersion | None:
        row = connection.execute(
            "SELECT exchange_code, session_date, session_class, state, opens_at, "
            "closes_at, source_url, source_published_at, known_at, reviewed_by, "
            "reviewed_at, session_id, recorded_at FROM calendar_sessions "
            "WHERE exchange_code = ? AND session_date = ? "
            "AND known_at <= ? AND reviewed_at <= ? AND recorded_at <= ? "
            "ORDER BY known_at DESC LIMIT 1",
            (
                exchange_code,
                session_date.isoformat(),
                as_of_text,
                as_of_text,
                as_of_text,
            ),
        ).fetchone()
        return _decode_session_version(row) if row is not None else None
