"""The Yahoo fundamentals ingest: what it writes, and what it refuses to write.

The engine's personas reason over these records, so the two things that matter
are (a) every amount is a figure Yahoo actually reported, and (b) a quarter is
not visible before SEBI would have let the company publish it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from bq.data.vendors.yahoo_fundamentals_vendor import (
    FILING_LAG_DAYS,
    YahooFundamentalsError,
    YahooFundamentalsIngest,
    parse_quote_summary,
)
from bq.infra.database import migrate

# Two reported quarters, exactly the shape Yahoo's quoteSummary returns.
FIXTURE = {
    "quoteSummary": {
        "result": [
            {
                "assetProfile": {
                    "sector": "Energy",
                    "industry": "Oil & Gas Refining & Marketing",
                },
                "price": {"longName": "Reliance Industries Limited"},
                "defaultKeyStatistics": {"sharesOutstanding": {"raw": 6_766_000_000}},
                "earningsHistory": {
                    "history": [
                        {
                            "quarter": {"raw": 1_722_470_400},  # 2024-08-01
                            "epsActual": {"raw": 28.5},
                            "epsEstimate": {"raw": 27.0},
                        },
                        {
                            "quarter": {"raw": 1_711_929_600},  # 2024-04-01
                            "epsActual": {"raw": 26.1},
                            "epsEstimate": {"raw": 27.2},
                        },
                    ]
                },
                "incomeStatementHistoryQuarterly": {
                    "incomeStatementHistory": [
                        {
                            "endDate": {"raw": 1_722_470_400},
                            "totalRevenue": {"raw": 2_500_000_000_000},
                            "netIncome": {"raw": 190_000_000_000},
                            "operatingIncome": {"raw": 300_000_000_000},
                        },
                        {
                            "endDate": {"raw": 1_711_929_600},
                            "totalRevenue": {"raw": 2_400_000_000_000},
                            "netIncome": {"raw": 170_000_000_000},
                            "operatingIncome": {"raw": 280_000_000_000},
                        },
                    ]
                },
                "balanceSheetHistoryQuarterly": {
                    "balanceSheetStatements": [
                        {
                            "endDate": {"raw": 1_722_470_400},
                            "totalStockholderEquity": {"raw": 800_000_000_000},
                            "cash": {"raw": 90_000_000_000},
                            "longTermDebt": {"raw": 250_000_000_000},
                        },
                        {
                            "endDate": {"raw": 1_711_929_600},
                            "totalStockholderEquity": {"raw": 780_000_000_000},
                            "cash": {"raw": 85_000_000_000},
                            "longTermDebt": {"raw": 260_000_000_000},
                        },
                    ]
                },
                "cashflowStatementHistoryQuarterly": {
                    "cashflowStatements": [
                        {
                            "endDate": {"raw": 1_722_470_400},
                            "totalCashFromOperatingActivities": {
                                "raw": 200_000_000_000
                            },
                            "capitalExpenditures": {"raw": -60_000_000_000},
                        },
                        {
                            "endDate": {"raw": 1_711_929_600},
                            "totalCashFromOperatingActivities": {
                                "raw": 180_000_000_000
                            },
                            "capitalExpenditures": {"raw": -55_000_000_000},
                        },
                    ]
                },
            }
        ]
    }
}


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return path


def _ingest(db, payload=FIXTURE, now=None) -> tuple:
    body = json.dumps(payload).encode()
    ingestor = YahooFundamentalsIngest(
        db, clock=_Clock(now or datetime(2025, 6, 1, tzinfo=UTC))
    )
    ingestor._fetch = lambda _symbol: body  # noqa: SLF001 - the network is not under test
    return ingestor, ingestor.ingest("RELIANCE")


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_a_malformed_envelope_is_rejected() -> None:
    with pytest.raises(YahooFundamentalsError):
        parse_quote_summary(b"not json")


def test_an_error_envelope_is_rejected() -> None:
    with pytest.raises(YahooFundamentalsError):
        parse_quote_summary(
            json.dumps({"quoteSummary": {"error": {"code": "x"}}}).encode()
        )


# ---------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------


def test_ingest_writes_facts_snapshots_and_earnings(db) -> None:
    _, report = _ingest(db)
    assert report.facts is True
    assert report.snapshots == 2
    assert report.earnings == 2
    assert report.reason is None


def test_snapshots_hold_the_reported_figures_not_derived_ones(db) -> None:
    """Every amount is a number Yahoo reported. An earlier draft derived revenue
    from net income to fill a gap; that would have made every ROE and margin in
    the app a fiction."""
    _ingest(db)
    with sqlite3.connect(db) as connection:
        revenue, net_income, equity = connection.execute(
            "SELECT revenue_paise, net_income_paise, book_value_paise "
            "FROM fundamental_snapshots ORDER BY period_end DESC LIMIT 1"
        ).fetchone()
    # 2.5e12 rupees * 100 = 2.5e14 paise, exactly as reported.
    assert revenue == 250_000_000_000_000
    assert net_income == 19_000_000_000_000
    assert equity == 80_000_000_000_000


def test_free_cash_flow_is_operating_cash_less_capex(db) -> None:
    _ingest(db)
    with sqlite3.connect(db) as connection:
        fcf = connection.execute(
            "SELECT fcf_paise FROM fundamental_snapshots ORDER BY period_end DESC LIMIT 1"
        ).fetchone()[0]
    # 200bn operating - 60bn capex = 140bn rupees = 1.4e13 paise.
    assert fcf == 14_000_000_000_000


def test_snapshots_are_labelled_as_the_vendors_not_a_filing(db) -> None:
    _ingest(db)
    with sqlite3.connect(db) as connection:
        sources = {
            row[0]
            for row in connection.execute("SELECT source FROM fundamental_snapshots")
        }
    assert sources == {"YAHOO"}


def test_facts_carry_the_sector_the_risk_limit_needs(db) -> None:
    _ingest(db)
    with sqlite3.connect(db) as connection:
        sector, name = connection.execute(
            "SELECT sector, company_name FROM company_facts WHERE symbol = 'RELIANCE'"
        ).fetchone()
    assert sector == "Energy"
    assert name == "Reliance Industries Limited"


def test_earnings_rows_carry_a_filing_date_after_the_period_end(db) -> None:
    """The PEAD analyst compares this date to a session; using the period end
    would let it fire weeks before the result was published."""
    _ingest(db)
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT report_period, filing_date FROM earnings_events ORDER BY report_period"
        ).fetchall()
    for period, filed in rows:
        assert filed > period
        from datetime import date

        gap = (date.fromisoformat(filed) - date.fromisoformat(period)).days
        assert gap == FILING_LAG_DAYS


# ---------------------------------------------------------------------------
# Point-in-time honesty
# ---------------------------------------------------------------------------


def test_a_quarter_is_not_visible_before_the_filing_lag(db) -> None:
    """Ingest run *before* a quarter could have been published writes nothing for
    it, rather than writing a row whose known_at is in the future."""
    early = datetime(2024, 8, 20, tzinfo=UTC)  # Q ended 1 Aug; filing lag 15 Sep
    _, report = _ingest(db, now=early)
    # The April quarter (filed 16 May) is knowable; the August one is not.
    assert report.snapshots == 1
    assert report.earnings == 1


def test_the_lag_boundary_is_where_it_says_it_is(db) -> None:
    just_before = datetime(2024, 9, 14, 23, 59, tzinfo=UTC)
    just_after = datetime(2024, 9, 16, tzinfo=UTC)
    _, early = _ingest(db, now=just_before)
    assert early.snapshots == 1
    _, late = _ingest(db, now=just_after)
    assert late.snapshots == 2


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_quarter_without_equity_is_skipped_not_guessed(db) -> None:
    """This store's invariants (and the ratios built on them) need positive
    equity. A negative-book quarter is skipped rather than stored as a number an
    LLM would divide by."""
    payload = json.loads(json.dumps(FIXTURE))
    statements = payload["quoteSummary"]["result"][0]["balanceSheetHistoryQuarterly"][
        "balanceSheetStatements"
    ]
    for row in statements:
        row["totalStockholderEquity"] = {"raw": -1}
    _, report = _ingest(db, payload=payload)
    assert report.snapshots == 0


def test_a_quarter_without_revenue_is_skipped_not_guessed(db) -> None:
    payload = json.loads(json.dumps(FIXTURE))
    statements = payload["quoteSummary"]["result"][0][
        "incomeStatementHistoryQuarterly"
    ]["incomeStatementHistory"]
    for row in statements:
        del row["totalRevenue"]
    _, report = _ingest(db, payload=payload)
    assert report.snapshots == 0


def test_an_error_response_becomes_a_reason_not_an_exception(db) -> None:
    """An ingest is a batch over many names; one name Yahoo does not carry must
    not abort the rest."""
    ingestor = YahooFundamentalsIngest(
        db, clock=_Clock(datetime(2025, 6, 1, tzinfo=UTC))
    )
    ingestor._fetch = lambda _symbol: (
        b'{"quoteSummary": {"error": {"code": "Not Found"}}}'
    )  # noqa: SLF001
    report = ingestor.ingest("RELIANCE")
    assert report.reason is not None
    assert report.snapshots == 0
