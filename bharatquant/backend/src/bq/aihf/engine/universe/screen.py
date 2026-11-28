"""Screen a universe rule and rank it for each strategy.

WHY a screen at all: an LLM analyst reading 2,000 companies every week is
neither affordable nor how a desk works. A desk runs a cheap, mechanical
screen and hands its analysts a short list. Here the screen is two layers:

- Eligibility is the mandate's: the filters the screener answers as of the
  session date, from filings public on or before it.
- Ranking is the desk's: every analyst declares the numbers it wants high
  or low (``AlphaModel.screen``), a strategy ranks on the union of its
  seats' factors, and the top of the rank is what its analysts read.

The composite is an average of percentile ranks, not of raw values or
z-scores: a P/E of 4,000 is one bad rank, not an outlier that moves a mean.

No LLM and no judgement here. The same inputs give the same record.
"""

from __future__ import annotations

import math
from bisect import bisect_left

from bq.aihf.engine.data.protocol import DataClient
from bq.aihf.engine.data.screener import OrderBy, ScreenerFields
from bq.aihf.engine.fund.spec import FundSpec
from bq.aihf.engine.signals import get_screen
from bq.aihf.engine.universe.models import CoverageRecord, Factor, PodCoverage


def cover(spec: FundSpec, as_of: str, data_client: DataClient, held: list[str]) -> CoverageRecord:
    """Ask the mandate's universe rule which names it holds on *as_of*, and
    rank them for each strategy.

    One screener call per factor field: sorting by a field returns every
    eligible company and adds that field where the company reports it, while
    filtering on the field would drop the companies that do not.
    """
    if spec.universe is None:
        raise ValueError(f"{spec.name}: this mandate has no universe rule to screen")
    fields: list[str] = []
    for strategy in spec.strategies:
        for factor in strategy.screen:
            if factor.field not in fields:
                fields.append(factor.field)

    eligible: set[str] = set()
    values: dict[str, dict[str, float]] = {}
    for field in fields:
        rows = data_client.screen(as_of, spec.universe.filters, order_by=OrderBy(field=field))
        eligible.update(row.ticker for row in rows)
        values[field] = {row.ticker: row.values[field] for row in rows if field in row.values}

    names = sorted(eligible)
    pods: dict[str, PodCoverage] = {}
    for strategy in spec.strategies:
        scores, unscored = rank(names, values, strategy.screen)
        ranked = list(scores)
        kept = ranked[:spec.universe.candidates]
        pods[strategy.name] = PodCoverage(
            factors=list(strategy.screen),
            candidates={name: scores[name] for name in kept},
            rejected=ranked[spec.universe.candidates:],
            unscored=unscored,
        )
    return CoverageRecord(as_of=as_of, held=sorted(held), pods=pods)


def rank(
    names: list[str], values: dict[str, dict[str, float]], factors: tuple[Factor, ...],
) -> tuple[dict[str, float], list[str]]:
    """Score every name on *factors* and order them, best first.

    *values* is field -> name -> value. A name's score is the mean of its
    percentiles on the factors it has a value for; a factor it lacks is
    left out of its mean. A name with no value for any factor has no score.
    Return the scores (best first; ties by name) and the names without one.
    """
    by_factor = [percentiles(values.get(factor.field, {}), factor.better) for factor in factors]
    scores: dict[str, float] = {}
    unscored: list[str] = []
    for name in names:
        have = [ranks[name] for ranks in by_factor if name in ranks]
        if have:
            scores[name] = sum(have) / len(have)
        else:
            unscored.append(name)
    ordered = dict(sorted(scores.items(), key=lambda item: (-item[1], item[0])))
    return ordered, sorted(unscored)


def percentiles(values: dict[str, float], better: str) -> dict[str, float]:
    """Each name's standing on one factor: the percent of the names with a
    value that it strictly beats. 0 is the worst; ties share a standing.
    A value that is not a finite number counts as no value."""
    worth = {name: _worth(value, better) for name, value in values.items() if math.isfinite(value)}
    ordered = sorted(worth.values())
    return {name: 100 * bisect_left(ordered, value) / len(ordered) for name, value in worth.items()}


def universe_problems(spec: FundSpec, fields: ScreenerFields) -> list[str]:
    """Every reason this mandate's universe rule would fail on the first
    rebalance day, in plain words. Empty means sound. *fields* is the
    screener's vocabulary (``DataClient.screener_fields``)."""
    if spec.universe is None:
        return []
    found = fields.problems(spec.universe.filters)
    analysts: list[str] = []
    for strategy in spec.strategies:
        for model in strategy.models:
            if model.name not in analysts:
                analysts.append(model.name)
    for analyst in analysts:
        for factor in get_screen(analyst):
            if factor.field not in fields.numeric:
                found.append(f"{analyst} screens on '{factor.field}', which is not a number the screener has")
            elif factor.field in fields.unavailable_as_of:
                found.append(f"{analyst} screens on '{factor.field}', which the screener cannot answer for a past date")
    return found


def _worth(value: float, better: str) -> float:
    """A number that is higher for a better value. On a lower-is-better
    factor a negative value is the worst there is: a P/E of -5 is a company
    losing money, not a bargain, and negative debt-to-equity is negative
    equity."""
    if better == "high":
        return value
    return -value if value >= 0 else -math.inf
