from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from bq.analysis.wiring import build_analysis_router
from bq.data.equity_master import (
    EquityMasterError,
    EquityMasterStore,
    normalise_company_name,
    parse_listing,
)
from bq.data.vendors.base import DataDomain
from bq.infra.database import migrate

NOW = datetime(2026, 11, 26, 3, 30, tzinfo=UTC)

HEADER = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, "
    "MARKET LOT, ISIN NUMBER, FACE VALUE\n"
)
CSV_TEXT = (
    HEADER
    + "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
    + "SUNPHARMA,Sun Pharmaceutical Industries Limited,EQ,08-JUL-1994,1,1,"
    "INE044A01036,1\n"
    + "BANKBARODA,Bank of Baroda,EQ,19-FEB-1997,2,1,INE028A01039,2\n"
    + "M&M,Mahindra & Mahindra Limited,EQ,01-JAN-1996,5,1,INE101A01026,5\n"
    + "SOMEBOND,Some Bond,N1,01-JAN-2001,10,1,INE000000000,10\n"
    + "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
    + "SHORT,Short Row,EQ\n"
)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def test_listing_keeps_only_resolvable_equities(tmp_path: Path) -> None:
    rows = parse_listing(CSV_TEXT.encode("utf-8"))
    symbols = [row.symbol for row in rows]
    assert symbols == ["RELIANCE", "SUNPHARMA", "BANKBARODA"]
    assert rows[1].company_name == "Sun Pharmaceutical Industries Limited"
    assert rows[1].series == "EQ"


def test_listing_rejects_a_malformed_header() -> None:
    with pytest.raises(EquityMasterError, match="missing"):
        parse_listing(b"SYMBOL,NAME\nRELIANCE,Reliance\n")


def test_listing_accepts_a_utf8_bom() -> None:
    payload = ("\ufeff" + CSV_TEXT).encode("utf-8")
    assert len(parse_listing(payload)) == 3


def test_company_name_normalisation_strips_only_legal_forms() -> None:
    assert (
        normalise_company_name("Sun Pharmaceutical Industries Limited")
        == "sun pharmaceutical"
    )
    assert normalise_company_name("Bank of Baroda") == "bank of baroda"
    assert normalise_company_name("The Tata Power Company Limited") == "tata power"


def test_store_versions_rows_and_aliases(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = EquityMasterStore(path, FrozenClock())

    assert store.refresh(CSV_TEXT.encode("utf-8"), as_of=date(2026, 11, 26)) == 3
    assert store.refresh(CSV_TEXT.encode("utf-8"), as_of=date(2026, 11, 26)) == 0
    assert store.latest_as_of() == date(2026, 11, 26)

    aliases = store.aliases()
    by_key = {(alias.key, alias.symbol): alias.kind for alias in aliases}
    assert by_key[("sun pharmaceutical", "SUNPHARMA")] == "name"
    assert by_key[("bank of baroda", "BANKBARODA")] == "name"
    assert by_key[("RELIANCE", "RELIANCE")] == "ticker"

    later = CSV_TEXT.replace(
        "Sun Pharmaceutical Industries Limited", "Sun Pharma Global Limited"
    )
    assert store.refresh(later.encode("utf-8"), as_of=date(2026, 11, 27)) == 3
    assert store.latest_as_of() == date(2026, 11, 27)
    before = {row.symbol: row for row in store.rows(as_of=date(2026, 11, 26))}
    after = {row.symbol: row for row in store.rows()}
    assert before["SUNPHARMA"].company_name.startswith("Sun Pharmaceutical")
    assert after["SUNPHARMA"].company_name == "Sun Pharma Global Limited"


def test_manual_input_cannot_mutate_the_listing(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = EquityMasterStore(path, FrozenClock())
    store.refresh(CSV_TEXT.encode("utf-8"), as_of=date(2026, 11, 26))
    with pytest.raises(EquityMasterError, match="calendar date"):
        store.refresh(CSV_TEXT.encode("utf-8"), as_of=datetime(2026, 11, 26))  # type: ignore[arg-type]
    with pytest.raises(EquityMasterError, match="HTTPS"):
        store.refresh(
            CSV_TEXT.encode("utf-8"), as_of=date(2026, 11, 26), source_url="http://x"
        )


def test_kite_less_router_serves_intraday_from_yahoo(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    router = build_analysis_router(path, SimpleNamespace(kite_enabled=False))
    chain = router.chain(DataDomain.BARS_INTRADAY)
    assert [vendor.name for vendor in chain] == ["yahoo"]
    assert DataDomain.BARS_INTRADAY in chain[0].capabilities


def test_raw_aliases_keep_brand_words(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    payload = (
        HEADER + "PTCIL,PTC Industries Limited,EQ,01-JAN-2020,10,1,INE000000002,10\n"
    ).encode("utf-8")
    store = EquityMasterStore(path, FrozenClock())
    store.refresh(payload, as_of=date(2026, 11, 26))
    keys = {alias.key for alias in store.aliases()}
    assert "ptc industries" in keys


def test_generic_words_do_not_create_short_prefixes(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    payload = (
        HEADER + "ONGC,Oil and Natural Gas Corporation Limited,EQ,01-JAN-1995,5,1,"
        "INE000000003,5\n"
    ).encode("utf-8")
    store = EquityMasterStore(path, FrozenClock())
    store.refresh(payload, as_of=date(2026, 11, 26))
    keys = {alias.key for alias in store.aliases()}
    assert "oil and" not in keys
    assert "oil natural" in keys
