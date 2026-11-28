"""What a universe rule says, and what one screen of it decided.

A mandate with a ``universe:`` block names no stocks. It names a rule: who
is eligible (filters the screener answers as of a date) and how many names
each strategy's screen keeps. On every rebalance session the engine asks
the rule again and writes down the answer, so a reader can see what the
fund picked, what it passed over, and what it still held.

Names here are "names", not tickers: the same record must be able to hold a
contract when the fund trades something other than stocks.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bq.aihf.engine.data.screener import COMPANY_FIELDS, ScreenFilter

# The only filter fields whose value is text. Everything else compares numbers.
TEXT_FIELDS = COMPANY_FIELDS | {"ticker"}


class Factor(BaseModel):
    """One number an analyst wants high or low in the names it is shown."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(description="a numeric screener field, e.g. 'return_on_equity'")
    better: Literal["high", "low"]


class UniverseSpec(BaseModel):
    """The rule a fund picks its names by."""

    model_config = ConfigDict(extra="forbid")

    filters: list[ScreenFilter] = Field(min_length=1, description="who is eligible, in the screener's vocabulary")
    candidates: int = Field(default=10, gt=0, description="names each strategy's screen keeps for research")

    @model_validator(mode="after")
    def _numbers_compare_to_numbers(self) -> UniverseSpec:
        # YAML reads `1e10` as text, and the filter's value accepts text for
        # the sake of `sector`. Without this the API is sent a string.
        for item in self.filters:
            values = item.value if isinstance(item.value, list) else [item.value]
            if item.field not in TEXT_FIELDS and any(isinstance(value, str) for value in values):
                raise ValueError(f"{item.field}: needs a number, got {item.value!r} (write 10000000000, not 1e10)")
        return self


class PodCoverage(BaseModel):
    """One strategy's screen: what it ranked on, what it kept, what it passed over."""

    factors: list[Factor]
    candidates: dict[str, float] = Field(description="name -> composite score (0-100), best first")
    rejected: list[str] = Field(description="every other ranked name, best first")
    unscored: list[str] = Field(description="eligible names with no value for any factor")

    @property
    def eligible(self) -> int:
        return len(self.candidates) + len(self.rejected) + len(self.unscored)


class CoverageRecord(BaseModel):
    """What one screen of the universe rule decided."""

    as_of: str = Field(description="the date the screen was asked about (YYYY-MM-DD)")
    held: list[str] = Field(description="names the fund held; they stay covered until sold")
    pods: dict[str, PodCoverage] = Field(description="strategy name -> its screen")

    @property
    def names(self) -> list[str]:
        """Every name any strategy researches."""
        covered = set(self.held)
        for pod in self.pods.values():
            covered.update(pod.candidates)
        return sorted(covered)

    def research(self, pod: str) -> list[str]:
        """The names one strategy's analysts read: its candidates, then the
        held names not among them. Screen for entries, hold list for exits."""
        candidates = list(self.pods[pod].candidates)
        return candidates + [name for name in self.held if name not in candidates]
