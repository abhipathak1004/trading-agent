"""Resolve due shadow episodes from later daily bars.

This closes the calibration loop: it never touches the decision or the plan, it
only appends an outcome. A BUY plan resolves on stop/target first-touch, then on
the close at the horizon. A no-plan HOLD resolves on the forward return using
the ``last_close`` stored in the episode detail. A later bar can never rewrite
the original episode.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from bq.core import Clock, SystemClock, paise_from_price, utc
from bq.data.symbols import SymbolError
from bq.data.symbols import resolve as resolve_symbol
from bq.data.vendors import Bar, BarInterval, DataDomain, VendorRouter
from bq.memory.episodic import Episode, EpisodeAction, EpisodicMemoryStore

DEFAULT_HORIZON_DAYS = 5
MAX_HORIZON_DAYS = 60
MAX_EPISODES = 200


class ResolutionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolutionReport:
    examined: int
    resolved: int
    skipped: int
    failed: int
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "examined": self.examined,
            "resolved": self.resolved,
            "skipped": self.skipped,
            "failed": self.failed,
            "notes": list(self.notes),
        }


class OutcomeResolver:
    def __init__(
        self,
        store: EpisodicMemoryStore,
        router: VendorRouter,
        *,
        clock: Clock | None = None,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        max_episodes: int = 50,
    ) -> None:
        if not 1 <= horizon_days <= MAX_HORIZON_DAYS:
            raise ResolutionError("Horizon days is out of range.")
        if not 1 <= max_episodes <= MAX_EPISODES:
            raise ResolutionError("Max episodes is out of range.")
        self.store = store
        self.router = router
        self.clock = clock or SystemClock()
        self.horizon = timedelta(days=horizon_days)
        self.max_episodes = max_episodes

    def resolve_due(self, *, as_of: datetime | None = None) -> ResolutionReport:
        pivot = utc(self.clock.now()) if as_of is None else utc(as_of)
        episodes = self.store.unresolved(
            due_before=pivot - self.horizon, limit=self.max_episodes
        )
        resolved = 0
        skipped = 0
        failed = 0
        notes: list[str] = []
        for episode in episodes:
            due_at = episode.as_of + self.horizon
            if due_at > pivot:
                skipped += 1
                continue
            try:
                self._resolve(episode, pivot=pivot, due_at=due_at)
                resolved += 1
            except ResolutionError as exc:
                skipped += 1
                notes.append(f"{episode.symbol}: {exc}")
            except Exception as exc:  # one bad vendor call must not stop the batch
                failed += 1
                notes.append(f"{episode.symbol}: {type(exc).__name__}")
        return ResolutionReport(
            examined=len(episodes),
            resolved=resolved,
            skipped=skipped,
            failed=failed,
            notes=tuple(notes),
        )

    def _resolve(self, episode: Episode, *, pivot: datetime, due_at: datetime) -> None:
        try:
            symbol = resolve_symbol(episode.symbol)
        except SymbolError as exc:
            raise ResolutionError("unresolvable symbol") from exc
        if not symbol.tradeable:
            raise ResolutionError("index episode cannot be resolved")
        start = episode.as_of
        end = min(pivot, due_at)
        result = self.router.bars(
            symbol,
            interval=BarInterval.DAY_1,
            start=start,
            end=end,
            domain=DataDomain.BARS_DAILY,
        )
        bars = tuple(
            bar for bar in result.data if episode.as_of < bar.event_at <= due_at
        )
        if not bars:
            raise ResolutionError("no bars after as_of")
        if episode.action is EpisodeAction.BUY and self._has_plan(episode):
            self._resolve_plan(episode, bars)
            return
        self._resolve_forward(episode, bars)

    @staticmethod
    def _has_plan(episode: Episode) -> bool:
        return (
            episode.entry_price_paise is not None
            and episode.stop_loss_paise is not None
            and episode.target_price_paise is not None
        )

    def _resolve_plan(self, episode: Episode, bars: tuple[Bar, ...]) -> None:
        entry = episode.entry_price_paise
        stop = episode.stop_loss_paise
        target = episode.target_price_paise
        assert entry is not None and stop is not None and target is not None
        risk = entry - stop
        if risk <= 0 or target <= entry:
            raise ResolutionError("invalid plan geometry")
        exit_price = Decimal(bars[-1].close)
        resolved_at = bars[-1].event_at
        outcome = 1 if exit_price > Decimal(entry) / 100 else 0
        realized = float((paise_from_price(exit_price).value - entry) / risk)
        for bar in bars:
            low = paise_from_price(bar.low).value
            high = paise_from_price(bar.high).value
            if low <= stop:
                exit_price = Decimal(bar.low)
                resolved_at = bar.event_at
                outcome = 0
                realized = -1.0
                break
            if high >= target:
                exit_price = Decimal(bar.high)
                resolved_at = bar.event_at
                outcome = 1
                realized = float((target - entry) / risk)
                break
        self.store.resolve(
            episode.episode_id,
            resolved_at=resolved_at,
            outcome=outcome,
            realized_r=realized,
            close_price_paise=paise_from_price(exit_price).value,
            source="outcome_resolver",
        )

    def _resolve_forward(self, episode: Episode, bars: tuple[Bar, ...]) -> None:
        raw = episode.detail.get("last_close")
        if raw is None:
            raise ResolutionError("no reference close for a no-plan episode")
        try:
            reference = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ResolutionError("invalid reference close") from exc
        if reference <= 0:
            raise ResolutionError("invalid reference close")
        last = bars[-1]
        close = float(last.close)
        if episode.action in (EpisodeAction.SELL, EpisodeAction.AVOID):
            outcome = 1 if close < reference else 0
        else:
            outcome = 1 if close > reference else 0
        self.store.resolve(
            episode.episode_id,
            resolved_at=last.event_at,
            outcome=outcome,
            realized_r=0.0,
            close_price_paise=paise_from_price(last.close).value,
            source="outcome_resolver",
        )


__all__ = [
    "DEFAULT_HORIZON_DAYS",
    "MAX_EPISODES",
    "MAX_HORIZON_DAYS",
    "OutcomeResolver",
    "ResolutionError",
    "ResolutionReport",
]
