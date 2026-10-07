"""Universe rebuilt from NSE's official index constituent CSVs (2026-10-08)."""
from __future__ import annotations

import subprocess
import sys

import pytest

from analysis.sector_classification import app_sector_from_nse_industry, classify_sector
from data import universe as u


@pytest.mark.parametrize("level,lo,hi", [
    ("nifty50", 50, 50), ("nifty100", 98, 102), ("nifty200", 150, 210),
    ("nifty500", 490, 505), ("niftytotalmarket", 780, 900),
])
def test_universe_sizes_and_no_duplicates(level, lo, hi):
    lst = u.get_universe(level)
    assert lo <= len(lst) <= hi, (level, len(lst))
    assert len(lst) == len(set(lst))
    assert all(t.endswith(".NS") for t in lst)


def test_tiers_nest():
    n50, n500, tm = (set(u.get_universe(k)) for k in ("nifty50", "nifty500", "all"))
    assert n50 <= n500 <= tm
    assert set(u.LIQUID_EXTRAS) <= tm


@pytest.mark.parametrize("sym", ["HAL", "JIOFIN", "INDIGO", "CUPID", "DHANBANK",
                                 "MAXHEALTH", "SHREECEM", "MAZDOCK"])
def test_audit_names_present(sym):
    assert f"{sym}.NS" in u.get_universe("niftytotalmarket")


def test_every_total_market_name_has_a_sector():
    other = [t for t in u.get_universe("all") if u.get_sector(t) == "Other"]
    assert other == []


def test_liquid_extras_have_sectors_and_no_index_overlap():
    for t in u.LIQUID_EXTRAS:
        assert u.get_sector(t) != "Other", t
    official = set(u.get_universe("nifty500")) | set(u.NIFTY_MICROCAP250) | set(u.NIFTY_TOTALMARKET_REST)
    assert not (set(u.LIQUID_EXTRAS) & official)


@pytest.mark.parametrize("ticker,group", [
    ("HDFCBANK", "Banks"), ("DHANBANK", "Banks"),       # banks
    ("BAJFINANCE", "Financial Services"), ("JIOFIN", "Financial Services"),  # NBFCs
    ("PFC", "Financial Services"),                       # PSU lender
])
def test_financials_classify_as_financial(ticker, group):
    prof = classify_sector(u.get_sector(ticker))
    assert prof.is_financial and prof.group == group


@pytest.mark.parametrize("ticker", ["HAL", "BEL", "COALINDIA"])   # PSU non-financials
def test_psu_operating_companies_not_financial(ticker):
    assert u.get_sector(ticker) != "Other"
    assert not classify_sector(u.get_sector(ticker)).is_financial


@pytest.mark.parametrize("industry,company,expected", [
    ("Financial Services", "Karnataka Bank Ltd.", "Banking"),
    ("Financial Services", "Bank of India", "Banking"),
    ("Financial Services", "Bajaj Finance Ltd.", "Finance"),
    ("Financial Services", "SBI Life Insurance Company Ltd.", "Insurance"),
    ("Healthcare", "Max Healthcare Institute Ltd.", "Healthcare"),
    ("Healthcare", "Cipla Ltd.", "Pharma"),
    ("Capital Goods", "Hindustan Aeronautics Ltd.", "CapitalGoods"),
    ("Something New", "X Ltd.", "Other"),
])
def test_nse_industry_mapping(industry, company, expected):
    assert app_sector_from_nse_industry(industry, company) == expected


def test_former_constituents_resolve_and_are_not_scanned():
    tm = set(u.get_universe("all"))
    assert not (set(u.FORMER_CONSTITUENTS) & tm)
    assert u.resolve_ticker("PFIZER") == "PFIZER.NS"


def test_import_does_no_network():
    code = (
        "import socket\n"
        "def _boom(*a, **k): raise RuntimeError('network at import')\n"
        "socket.socket.connect = _boom\n"
        "socket.create_connection = _boom\n"
        "socket.getaddrinfo = _boom\n"
        "import data.universe as u\n"
        "assert len(u.get_universe('all')) > 700\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=u.__file__.rsplit("data", 1)[0])
    assert r.returncode == 0, r.stderr[-2000:]
