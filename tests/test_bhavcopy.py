"""Bhavcopy parsing tests against a recorded CSV shape."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pinak.data.providers.nse_bhavcopy import BhavcopyError, NseBhavcopyProvider

FIXTURE = Path(__file__).parent / "fixtures" / "bhavcopy.csv"
FIXTURE_LACS = Path(__file__).parent / "fixtures" / "bhavcopy_lacs.csv"


def test_parse_keeps_only_eq_and_types_the_columns():
    frame = NseBhavcopyProvider._parse(FIXTURE.read_bytes(), date(2026, 11, 20))

    assert frame.height == 2
    assert set(frame["symbol"].to_list()) == {"RELIANCE", "NODELIV"}
    assert frame.schema["close"] == frame.schema["turnover"]

    reliance = frame.filter(frame["symbol"] == "RELIANCE").row(0, named=True)
    assert reliance["close"] == 1420.0
    assert reliance["turnover"] == 14_200_000_000.0
    assert reliance["delivery_pct"] == 40.0

    no_delivery = frame.filter(frame["symbol"] == "NODELIV").row(0, named=True)
    assert no_delivery["delivery_pct"] is None


def test_parse_normalises_lakh_turnover_to_rupees():
    frame = NseBhavcopyProvider._parse(FIXTURE_LACS.read_bytes(), date(2026, 11, 20))
    row = frame.row(0, named=True)
    assert row["turnover"] == 100_000.0


def test_parse_rejects_unknown_schema():
    with pytest.raises(BhavcopyError):
        NseBhavcopyProvider._parse(b"FOO,BAR\n1,2\n", date(2026, 11, 20))
