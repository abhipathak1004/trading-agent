from dataclasses import dataclass
from datetime import UTC, datetime


class ReplayError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReplayBar:
    instrument_id: str
    event_at: datetime
    known_at: datetime
    open_paise: int
    high_paise: int
    low_paise: int
    close_paise: int
    volume: int

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or not self.instrument_id:
            raise ReplayError("Invalid replay instrument.")
        for value in (
            self.open_paise,
            self.high_paise,
            self.low_paise,
            self.close_paise,
            self.volume,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReplayError("Replay bar values must be nonnegative integers.")
        if not 0 <= self.low_paise <= self.high_paise:
            raise ReplayError("Replay bar high/low range is invalid.")
        if not (
            self.low_paise <= self.open_paise <= self.high_paise
            and self.low_paise <= self.close_paise <= self.high_paise
        ):
            raise ReplayError("Replay open/close must fall inside the bar range.")
        if self.event_at.utcoffset() is None or self.known_at.utcoffset() is None:
            raise ReplayError("Replay timestamps must be timezone-aware.")
        if self.known_at.astimezone(UTC) < self.event_at.astimezone(UTC):
            raise ReplayError("Replay data cannot be known before it occurs.")


class ReplayCursor:
    def __init__(self, bars: tuple[ReplayBar, ...]) -> None:
        if not isinstance(bars, tuple) or len(bars) > 100_000:
            raise ReplayError("Replay bars must be a bounded tuple.")
        if any(not isinstance(bar, ReplayBar) for bar in bars):
            raise ReplayError("Replay cursor requires ReplayBar entries.")
        ordered = tuple(
            sorted(
                bars,
                key=lambda bar: (
                    bar.event_at.astimezone(UTC),
                    bar.known_at.astimezone(UTC),
                    bar.instrument_id,
                ),
            )
        )
        if tuple(bar.event_at.astimezone(UTC) for bar in bars) != tuple(
            bar.event_at.astimezone(UTC) for bar in ordered
        ):
            raise ReplayError("Replay bars must be ordered by event time.")
        self.bars = bars
        self.position = 0

    def advance(self, as_of: datetime) -> tuple[ReplayBar, ...]:
        if as_of.utcoffset() is None:
            raise ReplayError("Replay as-of must be timezone-aware.")
        instant = as_of.astimezone(UTC)
        visible: list[ReplayBar] = []
        while self.position < len(self.bars):
            bar = self.bars[self.position]
            if bar.known_at.astimezone(UTC) > instant:
                break
            visible.append(bar)
            self.position += 1
        return tuple(visible)
