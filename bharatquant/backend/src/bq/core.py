import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final, Protocol
from zoneinfo import ZoneInfo

EXCHANGE_TIMEZONE = ZoneInfo("Asia/Kolkata")

IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


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


def utc(value: datetime) -> datetime:
    """Return the instant in UTC, rejecting naive datetimes."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def timestamp(value: datetime) -> str:
    """Canonical microsecond ISO-8601 text, suffixed with ``Z``."""
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_digest(payload: object) -> str:
    """Stable sha256 over canonical JSON; rejects NaN and infinite numbers."""
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def open_connection(path: Path, *, foreign_keys: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=rw", uri=True, timeout=5
    )
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys = ON")
    return connection


def checked_identifier(value: str, message: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ValueError(message)
    return value


def paise_from_price(price: Decimal) -> CurrencyPaise:
    """Convert a positive price to exact paise, rounding half-up."""
    if not isinstance(price, Decimal):
        raise ValueError("Prices must be Decimal.")
    if price <= 0:
        raise ValueError("Prices must be positive.")
    paise = (price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return CurrencyPaise(int(paise))
