"""Yahoo's fundamentals and earnings, as a keyless source for the fund engine.

The ported engine's investor personas reason over point-in-time fundamentals and
its PEAD analyst fires on a filed earnings surprise. BharatQuant stores both
correctly but had **no populating feed at all** — the store existed and every
snapshot was owner-entered by hand. This module is that feed.

One request per symbol:

    quoteSummary?modules=assetProfile,defaultKeyStatistics,financialData,
                          earningsHistory,incomeStatementHistoryQuarterly,
                          balanceSheetHistoryQuarterly,cashflowStatementHistoryQuarterly,price

and the answer is turned into the three records the engine reads:
``CompanyFactsStore``, ``EarningsStore``, and
``ResearchExpansionStore.freeze_fundamentals``.

Everything written is a **reported figure from Yahoo's quarterly statements** —
no amount is derived from another to fill a hole. Where the vendor has no number
for a field the app's snapshot type requires, the record is not written: a
persona reading a made-up book value would compute a made-up ROE and reason
confidently about it, which is worse than abstaining.

Two rules that are load-bearing:

* **A filing date, not a fiscal period end.** Yahoo dates a quarter by its period
  end and gives no SEC acceptance time. A result is treated as knowable
  ``FILING_LAG_DAYS`` after that period end; SEBI's LODR requires quarterly
  results within 45 days, so that is the lag. Without it a backtest would see
  results weeks before they were published.
* **Yahoo's history is short.** It serves about four quarters, which is exactly
  the engine's 4-period floor and no more. A backtest longer than roughly a year
  will have personas abstain with ``InsufficientData`` for its earlier part. That
  limit is stated in ADR-053 and shown in the UI, not hidden here.

Nothing in this module decides whether to trade — it only records facts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote as url_quote

import httpx

from bq.aihf.data_store import CompanyFactsStore, EarningsStore
from bq.core import Clock
from bq.data.symbols import SymbolError, resolve, to_yahoo
from bq.fundamentals import FundamentalSnapshot, FundamentalSource
from bq.research_expansion import ResearchExpansionStore

QUOTE_SUMMARY_ENDPOINT = (
    "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
)
SOURCE_ID = "yahoo.fundamentals"
SOURCE_URL = "https://finance.yahoo.com"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
_USER_AGENT = "Mozilla/5.0 BharatQuant/0.1 (personal research)"

# SEBI LODR requires quarterly results within 45 days of the period end, so a
# filing cannot have been public before then. Every record's `known_at` uses it.
FILING_LAG_DAYS = 45

_MODULES = ",".join(
    (
        "assetProfile",
        "defaultKeyStatistics",
        "financialData",
        "earningsHistory",
        "incomeStatementHistoryQuarterly",
        "balanceSheetHistoryQuarterly",
        "cashflowStatementHistoryQuarterly",
        "price",
    )
)


class YahooFundamentalsError(ValueError):
    """Yahoo could not be read, or returned something outside the bounds."""


@dataclass(frozen=True, slots=True)
class IngestReport:
    """What one symbol's ingest wrote. Counts, never payloads."""

    symbol: str
    facts: bool
    snapshots: int
    earnings: int
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "facts": self.facts,
            "snapshots": self.snapshots,
            "earnings": self.earnings,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Yahoo's own shapes
# ---------------------------------------------------------------------------


def _raw(value: object) -> object:
    """Yahoo wraps every scalar as ``{"raw": x, "fmt": "..."}``; unwrap it."""
    if isinstance(value, dict):
        return value.get("raw")
    return value


def _paise(rupees: object) -> int | None:
    """A rupee amount as integer paise, or None when the vendor has no number."""
    raw = _raw(rupees)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int((Decimal(str(raw)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ArithmeticError, ValueError):
        return None


def _count(value: object) -> int | None:
    raw = _raw(value)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(Decimal(str(raw)))
    except (InvalidOperation, ArithmeticError, ValueError):
        return None


def _number(value: object) -> float | None:
    raw = _raw(value)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _date_raw(value: object) -> date | None:
    """A Yahoo date as a date: it serves either epoch seconds or an ISO string."""
    raw = _raw(value)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


def parse_quote_summary(payload: bytes) -> dict[str, object]:
    """Validate the envelope and return the ``result[0]`` module map."""
    try:
        body = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise YahooFundamentalsError("Yahoo response was not valid JSON.") from exc
    summary = body.get("quoteSummary") if isinstance(body, dict) else None
    if not isinstance(summary, dict) or summary.get("error"):
        raise YahooFundamentalsError("Yahoo reported an error for this symbol.")
    results = summary.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise YahooFundamentalsError("Yahoo returned no summary for this symbol.")
    return results[0]


def _statement_rows(
    summary: dict[str, object], module: str, key: str
) -> list[dict[str, object]]:
    """The rows of a quarterly statement module, keyed by period end."""
    section = summary.get(module)
    rows = section.get(key) if isinstance(section, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        row for row in rows if isinstance(row, dict) and _date_raw(row.get("endDate"))
    ]


def _by_period(
    rows: list[dict[str, object]],
) -> dict[date, dict[str, object]]:
    keyed: dict[date, dict[str, object]] = {}
    for row in rows:
        period = _date_raw(row.get("endDate"))
        if period is not None:
            keyed[period] = row
    return keyed


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class YahooFundamentalsIngest:
    """Fetch and store one symbol's facts, snapshots and earnings.

    One instance per run: the transport is reusable so fifty symbols do not open
    fifty pools. Each store is opened per call — they are short writes.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        clock: Clock,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._path = database_path
        self._clock = clock
        self._transport = transport
        self._timeout = timeout

    def ingest(self, symbol: str) -> IngestReport:
        """Read one symbol from Yahoo and write everything it supports.

        An uncovered symbol returns a report with a reason rather than raising:
        an ingest is a batch, and one name Yahoo does not carry must not abort
        the rest.
        """
        try:
            yahoo_symbol = to_yahoo(resolve(symbol))
        except SymbolError as exc:
            return self._skip(symbol, str(exc))
        try:
            payload = self._fetch(yahoo_symbol)
            summary = parse_quote_summary(payload)
        except YahooFundamentalsError as exc:
            return self._skip(symbol, str(exc))

        now = self._clock.now()
        facts = self._write_facts(symbol, summary, now)
        snapshots = self._write_snapshots(symbol, summary, now)
        earnings = self._write_earnings(symbol, summary, now)
        return IngestReport(
            symbol=symbol, facts=facts, snapshots=snapshots, earnings=earnings
        )

    # -- transport -------------------------------------------------------

    def _fetch(self, yahoo_symbol: str) -> bytes:
        url = QUOTE_SUMMARY_ENDPOINT.format(symbol=url_quote(yahoo_symbol))
        try:
            with httpx.Client(
                timeout=self._timeout, transport=self._transport
            ) as client:
                with client.stream(
                    "GET",
                    url,
                    params={"modules": _MODULES},
                    headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                ) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise YahooFundamentalsError(
                                "Yahoo response exceeded the size limit."
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
        except httpx.HTTPStatusError as exc:
            raise YahooFundamentalsError(
                f"Yahoo returned {exc.response.status_code}."
            ) from exc
        except httpx.HTTPError as exc:
            raise YahooFundamentalsError(
                f"Yahoo request failed: {type(exc).__name__}."
            ) from exc

    # -- writers ---------------------------------------------------------

    def _write_facts(
        self, symbol: str, summary: dict[str, object], now: datetime
    ) -> bool:
        profile = summary.get("assetProfile")
        section = profile if isinstance(profile, dict) else {}
        price = summary.get("price")
        price_section = price if isinstance(price, dict) else {}
        CompanyFactsStore(self._path).record(
            symbol=symbol,
            company_name=_text(price_section.get("longName"))
            or _text(price_section.get("shortName")),
            isin=None,  # Yahoo does not expose ISIN; equity_master owns it.
            sector=_text(section.get("sector")),
            industry=_text(section.get("industry")),
            known_at=now,
            source_id=SOURCE_ID,
            source_url=SOURCE_URL,
        )
        return True

    def _write_snapshots(
        self, symbol: str, summary: dict[str, object], now: datetime
    ) -> int:
        """One snapshot per quarter, from that quarter's reported statements.

        A quarter is written only when its income statement names revenue and net
        income, its balance sheet names equity, and the key statistics give a
        share count — because those are the four inputs the engine's ratios need
        and inventing any of them would make every number downstream wrong.
        """
        key = summary.get("defaultKeyStatistics")
        key_stats = key if isinstance(key, dict) else {}
        shares = _count(key_stats.get("sharesOutstanding"))
        if not shares or shares <= 0:
            return 0

        income = _by_period(
            _statement_rows(
                summary, "incomeStatementHistoryQuarterly", "incomeStatementHistory"
            )
        )
        balance = _by_period(
            _statement_rows(
                summary, "balanceSheetHistoryQuarterly", "balanceSheetStatements"
            )
        )
        cashflow = _by_period(
            _statement_rows(
                summary, "cashflowStatementHistoryQuarterly", "cashflowStatements"
            )
        )
        if not income:
            return 0

        store = ResearchExpansionStore(self._path, clock=self._clock)
        written = 0
        for period in sorted(income, reverse=True):
            known_at = _known_at(period)
            if known_at > now:
                continue
            snapshot = self._snapshot(
                symbol,
                period,
                known_at,
                shares,
                income[period],
                balance.get(period, {}),
                cashflow.get(period, {}),
            )
            if snapshot is None:
                continue
            try:
                store.freeze_fundamentals(snapshot)
            except Exception:  # noqa: BLE001 - one unusable quarter must not stop the rest
                continue
            written += 1
        return written

    def _snapshot(
        self,
        symbol: str,
        period: date,
        known_at: datetime,
        shares: int,
        income: dict[str, object],
        balance: dict[str, object],
        cashflow: dict[str, object],
    ) -> FundamentalSnapshot | None:
        revenue = _paise(income.get("totalRevenue"))
        net_income = _paise(income.get("netIncome"))
        equity = _paise(balance.get("totalStockholderEquity"))
        if revenue is None or revenue <= 0 or net_income is None or equity is None:
            return None
        if equity <= 0:
            # A negative book value is a real state, but this store's invariants
            # (and the ratios built on them) require positive equity. Skip the
            # quarter rather than store a number the model would divide by.
            return None

        # Operating income is the closest reported figure to EBITDA. Yahoo gives
        # no quarterly depreciation line, so it is used as-is and labelled that
        # way in the record's source, not silently presented as EBITDA.
        operating_income = _paise(income.get("operatingIncome"))
        ebitda = operating_income if operating_income is not None else abs(net_income)
        if ebitda < 0:
            ebitda = 0  # the store requires nonnegative EBITDA

        cash = _paise(balance.get("cash")) or _paise(
            balance.get("cashAndCashEquivalentsAtCarryingValue")
        )
        debt = _paise(balance.get("shortLongTermDebt")) or 0
        debt += _paise(balance.get("longTermDebt")) or 0
        # Free cash flow: reported operating cash flow less capex. Yahoo names
        # capex as a negative number already, so it is added, not subtracted.
        operating_cash = _paise(cashflow.get("totalCashFromOperatingActivities"))
        capex = _paise(cashflow.get("capitalExpenditures"))
        if operating_cash is None:
            fcf: int = net_income
        else:
            fcf = operating_cash + (capex or 0)

        return FundamentalSnapshot(
            instrument_id=symbol,
            source=FundamentalSource.YAHOO,
            period_end=datetime.combine(period, datetime.min.time(), tzinfo=UTC),
            known_at=known_at,
            revenue_paise=revenue,
            ebitda_paise=ebitda,
            net_income_paise=net_income,
            total_debt_paise=max(0, debt),
            cash_paise=max(0, cash or 0),
            shares_outstanding=shares,
            book_value_paise=equity,
            fcf_paise=fcf,
            period_label="quarter",
        )

    def _write_earnings(
        self, symbol: str, summary: dict[str, object], now: datetime
    ) -> int:
        """One row per quarter, so the PEAD analyst has a filed surprise to read.

        ``earningsHistory`` gives the actual and estimate EPS; the surprise label
        is left for the store to derive, so one rule decides BEAT vs MISS.
        """
        history = summary.get("earningsHistory")
        rows = history.get("history") if isinstance(history, dict) else None
        if not isinstance(rows, list):
            return 0
        store = EarningsStore(self._path, clock=self._clock)
        written = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            period = _date_raw(row.get("quarter"))
            if period is None:
                continue
            filing = period + timedelta(days=FILING_LAG_DAYS)
            known_at = _known_at(period)
            if known_at > now:
                continue
            inserted = store.record(
                symbol=symbol,
                report_period=period.isoformat(),
                filing_date=filing.isoformat(),
                eps_actual=_number(row.get("epsActual")),
                eps_estimate=_number(row.get("epsEstimate")),
                eps_surprise=None,
                known_at=known_at,
                source_type="RESULT",
                source_id=SOURCE_ID,
                source_url=SOURCE_URL,
            )
            written += 1 if inserted else 0
        return written

    @staticmethod
    def _skip(symbol: str, reason: str) -> IngestReport:
        return IngestReport(
            symbol=symbol, facts=False, snapshots=0, earnings=0, reason=reason
        )


def _known_at(period: date) -> datetime:
    """When a quarter's results could first have been public."""
    return datetime.combine(
        period + timedelta(days=FILING_LAG_DAYS), datetime.min.time(), tzinfo=UTC
    )


__all__ = [
    "FILING_LAG_DAYS",
    "IngestReport",
    "YahooFundamentalsError",
    "YahooFundamentalsIngest",
    "parse_quote_summary",
]
