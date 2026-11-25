from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

EXCHANGE_TIMEZONE = ZoneInfo("Asia/Kolkata")


def _validate_nonnegative_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 0:
        raise ValueError(f"{name} must not be negative.")


@dataclass(frozen=True, slots=True)
class CurrencyPaise:
    value: int

    def __post_init__(self) -> None:
        _validate_nonnegative_integer(self.value, "Paise")


@dataclass(frozen=True, slots=True)
class Quantity:
    value: int

    def __post_init__(self) -> None:
        _validate_nonnegative_integer(self.value, "Quantity")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def exchange_time(instant: datetime) -> datetime:
    if instant.utcoffset() is None:
        raise ValueError("Exchange time requires a timezone-aware instant.")
    return instant.astimezone(EXCHANGE_TIMEZONE)
