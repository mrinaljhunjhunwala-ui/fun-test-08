"""
Phase 0 fundamentals regression tests (Yahoo-only) — 29 tests covering schema
normalization, missing-field handling, the four analytics, the TTL cache, and
provider failures. No network: the Yahoo adapter's only network seam (`_fetch_raw`)
is monkeypatched with synthetic frames.

Run:  py -m pytest tests/test_fundamentals_phase0.py -q
"""
import logging
import os
import sys
from datetime import date

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from analysis.fundamentals import analytics as A           # noqa: E402
from analysis.fundamentals.cache import TTLCache           # noqa: E402
from analysis.fundamentals.models import (                 # noqa: E402
    BalanceSheet, CashFlow, CompanyFundamentals, FiscalPeriod, IncomeStatement,
    RatioSnapshot,
)
from analysis.fundamentals.provider import FundamentalProvider  # noqa: E402
from analysis.fundamentals.providers.yahoo_fundamentals import YahooFundamentalProvider  # noqa: E402
from analysis.fundamentals.service import FundamentalsService    # noqa: E402


# ── builders ────────────────────────────────────────────────────────────────
def _inc(year, revenue=None, eps=None, net_income=None, op_income=None):
    return IncomeStatement(
        period=FiscalPeriod(period_end=date(year, 3, 31), fiscal_year=year),
        revenue=revenue, eps_diluted=eps, net_income=net_income, operating_income=op_income)


def _bal(year, equity=None, debt=None, assets=None, cur_liab=None):
    return BalanceSheet(
        period=FiscalPeriod(period_end=date(year, 3, 31), fiscal_year=year),
        total_equity=equity, total_debt=debt, total_assets=assets, current_liabilities=cur_liab)


def _cf(symbol="TEST.NS", incs=None, bals=None, ratios=None, provider="YahooFinance"):
    return CompanyFundamentals(symbol=symbol, provider_name=provider,
                               income_statements=incs or [], balance_sheets=bals or [],
                               ratios=ratios)


# ── synthetic Yahoo frames (index=labels, columns=period-end dates, newest-first) ──
def _frame(data: dict, cols):
    return pd.DataFrame(data, index=cols).T


def _fake_raw():
    cols = [pd.Timestamp("2024-03-31"), pd.Timestamp("2023-03-31")]
    income = _frame({"Total Revenue": [1000.0, 800.0], "Net Income": [100.0, 80.0],
                     "Diluted EPS": [10.0, 8.0], "EBIT": [150.0, 120.0]}, cols)
    balance = _frame({"Total Assets": [2000.0, 1700.0], "Current Liabilities": [300.0, 250.0],
                      "Current Debt": [20.0, 15.0], "Long Term Debt": [80.0, 60.0],
                      "Stockholders Equity": [1000.0, 800.0]}, cols)
    cash = _frame({"Operating Cash Flow": [200.0, 150.0],
                   "Capital Expenditure": [-50.0, -40.0]}, cols)
    info = {"longName": "Test Co", "sector": "Tech", "returnOnEquity": 0.18,
            "debtToEquity": 45.0, "trailingPE": 20.0, "financialCurrency": "INR"}
    return {"income": income, "balance": balance, "cashflow": cash, "info": info}


@pytest.fixture
def yp(monkeypatch):
    p = YahooFundamentalProvider()
    monkeypatch.setattr(p, "_fetch_raw", lambda symbol, period: _fake_raw())
    return p


# ════════════════════════ schema / models ════════════════════════
def test_models_defaults_are_none_and_empty():
    cf = CompanyFundamentals(symbol="X.NS")
    assert cf.income_statements == [] and cf.balance_sheets == []
    assert cf.ratios is None and cf.is_partial is False and cf.missing_fields == []
    assert cf.has_any_data() is False


def test_missing_value_is_none_never_zero():
    s = IncomeStatement(period=FiscalPeriod(), revenue=None)
    assert s.revenue is None and s.net_income is None        # not 0


# ════════════════════════ TTL cache ════════════════════════
def test_cache_set_then_hit():
    c = TTLCache(ttl_seconds=100)
    c.set("k", 42)
    assert c.get("k") == 42 and c.hits == 1


def test_cache_miss_returns_none():
    c = TTLCache()
    assert c.get("absent") is None and c.misses == 1


def test_cache_expiry_is_a_miss():
    c = TTLCache(ttl_seconds=0)
    c.set("k", 1)
    assert c.get("k") is None        # already expired


def test_cache_stats():
    c = TTLCache()
    c.set("k", 1); c.get("k"); c.get("nope")
    s = c.stats()
    assert s["hits"] == 1 and s["misses"] == 1 and s["hit_rate"] == 0.5


# ════════════════════════ provider mapping ════════════════════════
def test_provider_income_mapping(yp):
    rows = yp.get_income_statement("TEST.NS")
    assert len(rows) == 2
    latest = rows[0]
    assert latest.revenue == 1000.0 and latest.net_income == 100.0
    assert latest.eps_diluted == 10.0 and latest.operating_income == 150.0
    assert latest.period.period_end == date(2024, 3, 31)


def test_provider_balance_total_debt_derived(yp):
    b = yp.get_balance_sheet("TEST.NS")[0]
    assert b.total_equity == 1000.0
    assert b.total_debt == 100.0          # 20 current + 80 long term, derived


def test_provider_cashflow_capex_abs_and_fcf_derived(yp):
    c = yp.get_cash_flow("TEST.NS")[0]
    assert c.capital_expenditure == 50.0          # sign normalized to positive
    assert c.free_cash_flow == 150.0              # 200 OCF - 50 capex


def test_provider_ratios_units(yp):
    r = yp.get_ratios("TEST.NS")
    assert r.roe == 0.18                          # fraction, unchanged
    assert r.debt_to_equity == 0.45               # percent 45 -> ratio 0.45


def test_provider_company_info(yp):
    info = yp.company_info("TEST.NS")
    assert info["company_name"] == "Test Co" and info["sector"] == "Tech"


def test_provider_empty_frames_return_empty_list(monkeypatch):
    p = YahooFundamentalProvider()
    monkeypatch.setattr(p, "_fetch_raw",
                        lambda symbol, period: {"income": pd.DataFrame(),
                                                "balance": pd.DataFrame(),
                                                "cashflow": pd.DataFrame(), "info": {}})
    assert p.get_income_statement("X.NS") == []
    assert p.get_balance_sheet("X.NS") == []


def test_provider_missing_label_yields_none(monkeypatch):
    p = YahooFundamentalProvider()
    cols = [pd.Timestamp("2024-03-31")]
    inc = _frame({"Total Revenue": [500.0]}, cols)   # no Net Income / EPS labels
    monkeypatch.setattr(p, "_fetch_raw",
                        lambda symbol, period: {"income": inc, "balance": pd.DataFrame(),
                                                "cashflow": pd.DataFrame(), "info": {}})
    row = p.get_income_statement("X.NS")[0]
    assert row.revenue == 500.0 and row.net_income is None and row.eps_diluted is None


def test_provider_transport_failure_raises(monkeypatch):
    p = YahooFundamentalProvider()
    def boom(symbol, period):
        raise ConnectionError("yahoo down")
    monkeypatch.setattr(p, "_fetch_raw", boom)
    with pytest.raises(RuntimeError):
        p.get_income_statement("X.NS")


def test_provider_raw_cache_single_fetch(monkeypatch):
    p = YahooFundamentalProvider()
    calls = {"n": 0}
    def counting(symbol, period):
        calls["n"] += 1
        return _fake_raw()
    monkeypatch.setattr(p, "_fetch_raw", counting)
    p.get_income_statement("TEST.NS"); p.get_balance_sheet("TEST.NS")
    p.get_cash_flow("TEST.NS"); p.get_ratios("TEST.NS")
    assert calls["n"] == 1                 # four get_* calls share one raw fetch


# ════════════════════════ analytics ════════════════════════
def test_revenue_cagr_basic():
    incs = [_inc(2024, revenue=1331), _inc(2023, revenue=1210),
            _inc(2022, revenue=1100), _inc(2021, revenue=1000)]
    r = A.revenue_cagr(_cf(incs=incs))
    assert r.available and r.value == pytest.approx(10.0, abs=0.3)
    assert r.unit == "%" and r.confidence in ("high", "medium")


def test_revenue_cagr_insufficient_points():
    r = A.revenue_cagr(_cf(incs=[_inc(2024, revenue=1000)]))
    assert not r.available and r.value is None and "≥2" in r.reason


def test_revenue_cagr_negative_start_undefined():
    incs = [_inc(2024, revenue=1000), _inc(2021, revenue=-50)]
    r = A.revenue_cagr(_cf(incs=incs))
    assert not r.available and r.value is None and "non-positive" in r.reason


def test_revenue_yoy_uses_last_two_statements_only():
    # 5y CAGR here is ~10%, but the latest year grew 25%: YoY must say 25.
    incs = [_inc(2024, revenue=1500), _inc(2023, revenue=1200),
            _inc(2022, revenue=1100), _inc(2021, revenue=1000)]
    r = A.revenue_yoy(_cf(incs=incs))
    assert r.available and r.value == pytest.approx(25.0, abs=0.01) and r.unit == "%"


def test_revenue_yoy_missing_or_gapped_is_unavailable():
    assert not A.revenue_yoy(_cf(incs=[_inc(2024, revenue=1000)])).available
    gapped = A.revenue_yoy(_cf(incs=[_inc(2024, revenue=1300), _inc(2021, revenue=1000)]))
    assert not gapped.available and gapped.value is None
    neg = A.revenue_yoy(_cf(incs=[_inc(2024, revenue=1000), _inc(2023, revenue=-5)]))
    assert not neg.available


def test_eps_cagr_basic():
    incs = [_inc(2024, eps=13.31), _inc(2023, eps=12.1),
            _inc(2022, eps=11.0), _inc(2021, eps=10.0)]
    r = A.eps_cagr(_cf(incs=incs))
    assert r.available and r.value == pytest.approx(10.0, abs=0.3)


def test_eps_cagr_negative_undefined():
    incs = [_inc(2024, eps=5.0), _inc(2021, eps=-2.0)]
    r = A.eps_cagr(_cf(incs=incs))
    assert not r.available and r.value is None


def test_roe_from_statements_uses_avg_equity():
    incs = [_inc(2024, net_income=100)]
    bals = [_bal(2024, equity=1000), _bal(2023, equity=800)]
    r = A.roe(_cf(incs=incs, bals=bals))
    assert r.available and r.value == pytest.approx(100 / 900 * 100, abs=0.1)  # avg equity 900
    assert r.confidence == "high"


def test_roe_negative_equity_unavailable():
    r = A.roe(_cf(incs=[_inc(2024, net_income=50)], bals=[_bal(2024, equity=-10)]))
    assert not r.available and r.value is None and "non-positive" in r.reason


def test_roe_fallback_to_vendor_ratio():
    cf = _cf(incs=[_inc(2024)], bals=[], ratios=RatioSnapshot(roe=0.2))
    r = A.roe(cf)
    assert r.available and r.value == pytest.approx(20.0) and r.confidence == "medium"


def test_debt_to_equity_from_statements():
    r = A.debt_to_equity(_cf(bals=[_bal(2024, equity=1000, debt=100)]))
    assert r.available and r.value == pytest.approx(0.10) and r.unit == "x"


def test_debt_to_equity_zero_equity_unavailable():
    r = A.debt_to_equity(_cf(bals=[_bal(2024, equity=0, debt=100)]))
    assert not r.available and r.value is None


def test_analytics_never_zero_on_empty():
    cf = _cf(incs=[], bals=[])
    for res in A.compute_all(cf).values():
        assert res.available is False and res.value is None     # never 0


# ════════════════════════ service + provider failures ════════════════════════
class _FakeProvider(FundamentalProvider):
    name = "Fake"
    def get_income_statement(self, symbol, period="annual", limit=10):
        return [_inc(2024, revenue=1100, eps=11, net_income=100),
                _inc(2023, revenue=1000, eps=10, net_income=90)]
    def get_balance_sheet(self, symbol, period="annual", limit=10):
        return [_bal(2024, equity=1000, debt=200)]
    def get_cash_flow(self, symbol, period="annual", limit=10):
        return []
    def get_ratios(self, symbol):
        return RatioSnapshot(roe=0.1)


class _RaisingProvider(FundamentalProvider):
    name = "Raiser"
    def get_income_statement(self, *a, **k): raise RuntimeError("transport")
    def get_balance_sheet(self, *a, **k): raise RuntimeError("transport")
    def get_cash_flow(self, *a, **k): raise RuntimeError("transport")
    def get_ratios(self, *a, **k): raise RuntimeError("transport")


def test_service_assembles_with_provenance():
    svc = FundamentalsService(providers=[_FakeProvider()])
    cf = svc.get_fundamentals("TEST.NS")
    assert cf.provider_name == "Fake" and cf.has_any_data()
    assert cf.statement_date == date(2024, 3, 31)
    assert cf.provenance.get("income.revenue") == "Fake"
    assert A.revenue_cagr(cf).available


def test_service_marks_partial_when_inputs_missing():
    # provider with no balance sheet → total_equity/total_debt missing → is_partial
    class _NoBalance(_FakeProvider):
        def get_balance_sheet(self, *a, **k): return []
    cf = FundamentalsService(providers=[_NoBalance()]).get_fundamentals("X.NS")
    assert cf.is_partial and "balance.total_equity" in cf.missing_fields


def test_service_provider_failure_returns_explicit_partial():
    cf = FundamentalsService(providers=[_RaisingProvider()]).get_fundamentals("X.NS")
    assert cf.provider_name is None and cf.is_partial
    assert "income_statements" in cf.missing_fields
    assert not cf.has_any_data()


def test_service_caches_second_call(monkeypatch):
    fake = _FakeProvider()
    calls = {"n": 0}
    orig = fake.get_income_statement
    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    fake.get_income_statement = counting
    svc = FundamentalsService(providers=[fake])
    svc.get_fundamentals("TEST.NS"); svc.get_fundamentals("TEST.NS")
    assert calls["n"] == 1                 # second call served from cache


def test_service_fallback_to_second_provider():
    svc = FundamentalsService(providers=[_RaisingProvider(), _FakeProvider()])
    cf = svc.get_fundamentals("X.NS")
    assert cf.provider_name == "Fake" and cf.has_any_data()   # tiering works


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
