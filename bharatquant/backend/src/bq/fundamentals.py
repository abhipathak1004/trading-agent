"""Deterministic fundamental ratios and DCF valuation.

All arithmetic lives here. Research notes and model text may describe a thesis,
but they never supply or override these numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from bq.core import IDENTIFIER, canonical_digest


class FundamentalError(ValueError):
    pass


class FundamentalSource(StrEnum):
    XBRL = "XBRL"
    OWNER = "OWNER"
    # Yahoo's quoteSummary. Labelling these rows XBRL would misstate where the
    # numbers came from, and a reader needs to know: XBRL is a filing, Yahoo is
    # a vendor's restatement of one (ADR-053).
    YAHOO = "YAHOO"


@dataclass(frozen=True, slots=True)
class FundamentalSnapshot:
    instrument_id: str
    source: FundamentalSource
    period_end: datetime
    known_at: datetime
    revenue_paise: int
    ebitda_paise: int
    net_income_paise: int
    total_debt_paise: int
    cash_paise: int
    shares_outstanding: int
    book_value_paise: int
    fcf_paise: int
    # Which kind of reporting period this row is. The fund engine asks for
    # trailing-twelve-month rows specifically; without a label a reader cannot
    # tell a TTM row from a single quarter. None means "unlabelled" (every row
    # written before migration 042).
    period_label: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or not IDENTIFIER.fullmatch(
            self.instrument_id
        ):
            raise FundamentalError("Invalid instrument identifier.")
        if not isinstance(self.source, FundamentalSource):
            raise FundamentalError("Invalid fundamental source.")
        if self.period_label not in (None, "ttm", "quarter", "annual"):
            raise FundamentalError("Invalid fundamental period label.")
        if self.period_end.utcoffset() is None or self.known_at.utcoffset() is None:
            raise FundamentalError("Fundamental timestamps must be timezone-aware.")
        if self.known_at < self.period_end:
            raise FundamentalError("Known-at cannot precede the period end.")
        for label, value in (
            ("revenue", self.revenue_paise),
            ("ebitda", self.ebitda_paise),
            ("total_debt", self.total_debt_paise),
            ("cash", self.cash_paise),
            ("book_value", self.book_value_paise),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FundamentalError(f"{label} must be a nonnegative integer.")
        if isinstance(self.net_income_paise, bool) or not isinstance(
            self.net_income_paise, int
        ):
            raise FundamentalError("Net income must be an integer.")
        if isinstance(self.fcf_paise, bool) or not isinstance(self.fcf_paise, int):
            raise FundamentalError("Free cash flow must be an integer.")
        if (
            isinstance(self.shares_outstanding, bool)
            or not isinstance(self.shares_outstanding, int)
            or self.shares_outstanding <= 0
        ):
            raise FundamentalError("Shares outstanding must be positive.")
        if self.book_value_paise <= 0:
            raise FundamentalError("Book value must be positive.")

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "source": self.source.value,
            "period_end": self.period_end.isoformat(),
            "known_at": self.known_at.isoformat(),
            "revenue_paise": str(self.revenue_paise),
            "ebitda_paise": str(self.ebitda_paise),
            "net_income_paise": str(self.net_income_paise),
            "total_debt_paise": str(self.total_debt_paise),
            "cash_paise": str(self.cash_paise),
            "shares_outstanding": self.shares_outstanding,
            "book_value_paise": str(self.book_value_paise),
            "fcf_paise": str(self.fcf_paise),
            "period_label": self.period_label,
        }

    @property
    def content_hash(self) -> str:
        return canonical_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class FundamentalRatios:
    market_cap_paise: int
    pe_bps: int | None
    pb_bps: int | None
    roe_bps: int | None
    debt_equity_bps: int | None
    ebitda_margin_bps: int | None
    net_margin_bps: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "market_cap_paise": str(self.market_cap_paise),
            "pe_bps": self.pe_bps,
            "pb_bps": self.pb_bps,
            "roe_bps": self.roe_bps,
            "debt_equity_bps": self.debt_equity_bps,
            "ebitda_margin_bps": self.ebitda_margin_bps,
            "net_margin_bps": self.net_margin_bps,
        }


@dataclass(frozen=True, slots=True)
class ValuationAssumptions:
    growth_bps: int = 1_000
    discount_bps: int = 1_200
    terminal_growth_bps: int = 400
    years: int = 5

    def __post_init__(self) -> None:
        for label, value in (
            ("growth", self.growth_bps),
            ("discount", self.discount_bps),
            ("terminal_growth", self.terminal_growth_bps),
            ("years", self.years),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise FundamentalError(f"{label} must be an integer.")
        if not 0 <= self.growth_bps <= 5_000:
            raise FundamentalError("Growth is outside its bound.")
        if not 1 <= self.discount_bps <= 5_000:
            raise FundamentalError("Discount rate is outside its bound.")
        if not 0 <= self.terminal_growth_bps <= 1_000:
            raise FundamentalError("Terminal growth is outside its bound.")
        if self.discount_bps <= self.terminal_growth_bps:
            raise FundamentalError("Discount rate must exceed terminal growth.")
        if not 1 <= self.years <= 20:
            raise FundamentalError("DCF years is outside its bound.")


@dataclass(frozen=True, slots=True)
class DcfResult:
    assumptions: ValuationAssumptions
    enterprise_value_paise: int
    equity_value_paise: int
    per_share_paise: int
    upside_bps: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "assumptions": {
                "growth_bps": self.assumptions.growth_bps,
                "discount_bps": self.assumptions.discount_bps,
                "terminal_growth_bps": self.assumptions.terminal_growth_bps,
                "years": self.assumptions.years,
            },
            "enterprise_value_paise": str(self.enterprise_value_paise),
            "equity_value_paise": str(self.equity_value_paise),
            "per_share_paise": str(self.per_share_paise),
            "upside_bps": self.upside_bps,
        }


def _bps(numerator: int, denominator: int) -> int | None:
    if denominator == 0:
        return None
    return numerator * 10_000 // denominator


def compute_ratios(
    snapshot: FundamentalSnapshot, *, price_paise: int
) -> FundamentalRatios:
    if (
        isinstance(price_paise, bool)
        or not isinstance(price_paise, int)
        or price_paise <= 0
    ):
        raise FundamentalError("A positive market price is required.")
    market_cap = price_paise * snapshot.shares_outstanding
    eps_paise = snapshot.net_income_paise // snapshot.shares_outstanding
    debt_equity = _bps(snapshot.total_debt_paise, snapshot.book_value_paise)
    return FundamentalRatios(
        market_cap_paise=market_cap,
        pe_bps=(_bps(price_paise, eps_paise) if eps_paise > 0 else None),
        pb_bps=_bps(market_cap, snapshot.book_value_paise),
        roe_bps=(
            _bps(snapshot.net_income_paise, snapshot.book_value_paise)
            if snapshot.net_income_paise > 0
            else None
        ),
        debt_equity_bps=debt_equity,
        ebitda_margin_bps=(
            _bps(snapshot.ebitda_paise, snapshot.revenue_paise)
            if snapshot.revenue_paise > 0
            else None
        ),
        net_margin_bps=(
            _bps(snapshot.net_income_paise, snapshot.revenue_paise)
            if snapshot.revenue_paise > 0
            else None
        ),
    )


def dcf_valuation(
    snapshot: FundamentalSnapshot,
    *,
    price_paise: int | None = None,
    assumptions: ValuationAssumptions | None = None,
) -> DcfResult:
    if snapshot.fcf_paise <= 0:
        raise FundamentalError("DCF requires positive free cash flow.")
    params = assumptions or ValuationAssumptions()
    if price_paise is not None and (
        isinstance(price_paise, bool)
        or not isinstance(price_paise, int)
        or price_paise <= 0
    ):
        raise FundamentalError("A positive market price is required.")
    growth = Decimal(params.growth_bps) / Decimal(10_000)
    discount = Decimal(params.discount_bps) / Decimal(10_000)
    terminal_growth = Decimal(params.terminal_growth_bps) / Decimal(10_000)
    base = Decimal(snapshot.fcf_paise)
    present = Decimal(0)
    for year in range(1, params.years + 1):
        cash = base * (Decimal(1) + growth) ** year
        present += cash / (Decimal(1) + discount) ** year
    final_cash = base * (Decimal(1) + growth) ** params.years
    terminal = (
        final_cash * (Decimal(1) + terminal_growth) / (discount - terminal_growth)
    )
    terminal_present = terminal / (Decimal(1) + discount) ** params.years
    enterprise = (present + terminal_present).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    equity = enterprise + snapshot.cash_paise - snapshot.total_debt_paise
    per_share = (equity / Decimal(snapshot.shares_outstanding)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    upside = (
        int(
            (
                (Decimal(per_share) - price_paise) / Decimal(price_paise) * 10_000
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if price_paise is not None
        else None
    )
    return DcfResult(
        assumptions=params,
        enterprise_value_paise=int(enterprise),
        equity_value_paise=int(equity),
        per_share_paise=int(per_share),
        upside_bps=upside,
    )


__all__ = [
    "DcfResult",
    "FundamentalError",
    "FundamentalRatios",
    "FundamentalSnapshot",
    "FundamentalSource",
    "ValuationAssumptions",
    "compute_ratios",
    "dcf_valuation",
]
