"""Sector mapping corrections from the 2026-10-03 audit.

Running all 793 universe tickers through classify_sector() showed:
hotels classed as Healthcare ("hospital" matched "Hospitality"); lenders PFC,
REC, IRFC, HUDCO filed under Energy / Capital Goods and so given industrial
leverage warnings; banks and NBFCs sitting in "Other" (same warnings); and
hospitals, cement makers and pharma names in "Other".
"""
from collections import Counter

import pytest

from analysis.sector_classification import CONSUMER, HEALTHCARE, classify_sector
from data import universe as U


def test_hospitality_is_not_healthcare():
    assert classify_sector("Hospitality").group == CONSUMER
    assert classify_sector("Hospitals").group == HEALTHCARE
    assert classify_sector("Hospital & Healthcare Services").group == HEALTHCARE


@pytest.mark.parametrize("sym,sector", [
    # lenders filed under operating sectors
    ("PFC", "Finance"), ("RECLTD", "Finance"), ("IRFC", "Finance"), ("HUDCO", "Finance"),
    ("CREDITACC", "Finance"),   # NBFC-MFI, not a bank
    # banks / financials that were "Other"
    ("CSBBANK", "Banking"), ("KTKBANK", "Banking"), ("TMB", "Banking"), ("JSFB", "Banking"),
    ("FEDFINA", "Finance"), ("IIFLCAPS", "Finance"), ("RELIGARE", "Finance"),
    ("SHAREINDIA", "Finance"), ("PRUDENT", "Finance"), ("TSFINV", "Finance"),
    ("CRAMC", "Finance"), ("MAHSCOOTER", "Finance"),
    ("ICICIGI", "Insurance"), ("ICICIPRULI", "Insurance"), ("UJJIVANSFB", "Banking"),
    # operating businesses that were "Other"
    ("PARKHOSPS", "Healthcare"), ("HCG", "Healthcare"), ("THYROCARE", "Healthcare"),
    ("AGARWALEYE", "Healthcare"),
    ("BIRLACORPN", "Cement"), ("ORIENTCEM", "Cement"), ("JKLAKSHMI", "Cement"),
    ("AARTIDRUGS", "Pharma"), ("SPARC", "Pharma"), ("SHILPAMED", "Pharma"),
    ("ALIVUS", "Pharma"), ("AKUMS", "Pharma"),
])
def test_corrected_assignments(sym, sector):
    assert U.get_sector(f"{sym}.NS") == sector


@pytest.mark.parametrize("sector", ["Banking", "Finance", "Insurance"])
def test_financial_sectors_never_get_leverage_warnings(sector):
    for tk in U.get_tickers_by_sector(sector):
        prof = classify_sector(U.get_sector(tk))
        assert prof.is_financial and not prof.leverage_warning_applies, tk


def test_no_ticker_is_assigned_to_two_sectors():
    counts = Counter(t for ts in U._SECTOR_ASSIGNMENTS.values() for t in ts)
    dupes = sorted(t for t, n in counts.items() if n > 1)
    assert not dupes, dupes
