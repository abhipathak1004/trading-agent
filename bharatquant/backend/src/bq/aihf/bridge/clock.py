"""The engine's clock, pinned to the NSE session instead of New York's.

The engine asks time three questions through the ``Clock`` protocol in
``engine/pipeline/clock.py``: may this session be looked at yet, which session's
information does a decision executing now come from, and what is everything
worth at a session. Upstream answers with New York dates. This module answers
with IST dates from ``bq.aihf.bridge.calendar`` and prices from the injected data
client, so the engine's own logic is untouched.

Why not just patch the vendored clock: the market assumption is a *policy* this
app owns (which calendar, which close time, which holidays), while the vendored
file also holds bar arithmetic that is market-agnostic. Keeping the policy here
means a future upstream diff does not collide with it, and a second market is a
second module rather than an edit to vendored source.

Intraday fundamentals are not supported in this pass (mandates with
``rebalance: minute`` are rejected at validation), so only the daily clock is
wired up; the bar clocks are left in the vendored code for a later phase.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from bq.aihf.bridge import calendar as market
from bq.aihf.engine.data.protocol import DataClient
from bq.aihf.engine.pipeline.clock import Marked

if TYPE_CHECKING:
    from bq.aihf.engine.fund.spec import FundSpec

# A session's mark is the close of the last daily bar on or before it. A fund
# values its book daily, so this is the only price the engine needs from the
# clock for a daily/weekly/monthly mandate.
_MARK_LOOKBACK_DAYS = 10


class NseDailyClock:
    """Sessions are completed IST trading dates; marks are IST daily closes.

    Satisfies the engine's ``Clock`` protocol structurally — no inheritance, the
    engine's own protocol is what matters.
    """

    def __init__(
        self, data: DataClient, *, gate: Callable[[str], bool] | None = None
    ) -> None:
        self._data = data
        self._gate = gate

    # ------------------------------------------------------------------
    # Clock protocol
    # ------------------------------------------------------------------

    def is_complete(self, session: str) -> bool:
        """A session may be looked at once its IST close has passed.

        An intraday key (``YYYY-MM-DDTHH:MM:SS``) is never produced by a
        daily-cadence mandate, so it is rejected rather than silently treated as
        a date: a bar session on the wrong clock would be valued at a close.
        """
        if len(session) > 10:
            raise market.MarketError(
                f"{session}: intraday sessions are not supported by the daily clock"
            )
        if self._gate is not None and not self._gate(session):
            return False
        return session <= market.completed_through()

    def previous(self, session: str) -> str:
        """The information cutoff for a decision executing at *session*: the
        prior trading date, which is what `execute_decision` refreshes from."""
        return market.previous_day(session)

    def marks(self, tickers: list[str], session: str) -> Marked:
        """A finite, positive close for every name at exactly *session*.

        The engine requires this to raise when a name cannot be marked: a
        missing close must not silently value a book at zero. The bridge data
        client returns bars only for real sessions, so a holiday raises here
        instead of producing an empty book.
        """
        if not self.is_complete(session):
            raise market.MarketError(f"{session}: that session is not complete yet")
        prices: dict[str, float] = {}
        for ticker in sorted(set(tickers)):
            close = self._close_on(ticker, session)
            if close is None:
                raise market.MarketError(
                    f"{ticker}: no close on {session}; cannot value the book or execute"
                )
            prices[ticker] = close
        return Marked(prices=prices)

    def session_date(self, session: str) -> str:
        return session[:10]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _close_on(self, ticker: str, session: str) -> float | None:
        """The close on *session* exactly, else None.

        Exact, not "most recent on or before": the engine already asked
        `is_complete`, so a missing bar here means the name did not trade —
        which the caller must see rather than paper over with a stale close.
        """
        start = market.previous_day(session)
        bars = self._data.get_prices(ticker, start, session)
        matches = [bar for bar in bars if bar.time[:10] == session]
        if not matches:
            return None
        return max(matches, key=lambda bar: bar.time).close


def clock_for_daily(
    data: DataClient, *, gate: Callable[[str], bool] | None = None
) -> NseDailyClock:
    """The clock a daily/weekly/monthly NSE mandate runs on."""
    return NseDailyClock(data, gate=gate)


def nse_clock_factory(
    *, gate: Callable[[str], bool] | None = None
) -> Callable[["FundSpec", DataClient, bool], "NseDailyClock"]:
    """A ``(spec, data, live) -> Clock`` factory for the engine's `clock_for`.

    Installed once by `bq/aihf/service.py`. It honours the mandate's cadence: a
    daily/weekly/monthly fund gets the IST daily clock; an intraday mandate is
    refused at validation in this pass, but the check stays here too so a
    hand-built spec cannot be silently valued at daily closes.
    """

    def factory(spec: "FundSpec", data: DataClient, live: bool) -> NseDailyClock:  # noqa: ARG001
        if spec.is_intraday:
            raise market.MarketError(
                "intraday mandates are not supported by the NSE daily clock"
            )
        return NseDailyClock(data, gate=gate)

    return factory


__all__ = ["NseDailyClock", "clock_for_daily", "nse_clock_factory"]
