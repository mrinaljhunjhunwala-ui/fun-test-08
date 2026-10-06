"""Analyze Stock (mockup artboard 02): a scored ticker renders the ticker
head, the tab strip with Overview first, the posture hero with four pillar
bars, the combined-read strip, the thesis grid, and the right-hand cards.
The fundamentals card is sector-aware: P/E leads for an energy name, P/B and
ROE lead for a bank and the leverage row disappears.

Scoring, price history, deep confirmation, TQS, thesis and fundamentals are
stubbed; network is blocked.
"""
import os
import socket
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAGE = os.path.join(_ROOT, "dashboard", "pages", "04_analyze_stock.py")

_TABS = ["Overview", "Fundamentals", "Chart", "Thesis",
         "News & Flags", "Corp Actions", "Portfolio Fit"]


def _prices():
    from utils.indicators import add_all_indicators
    idx = pd.bdate_range("2025-07-01", periods=300)
    rng = np.random.default_rng(3)
    close = 1000 + np.cumsum(rng.normal(0.8, 8, len(idx)))
    df = pd.DataFrame({"Open": close - 2, "High": close + 6, "Low": close - 6,
                       "Close": close, "Volume": rng.integers(2e5, 6e5, len(idx)).astype(float)},
                      index=idx)
    return add_all_indicators(df)


def _score(ticker, sector, company):
    from analysis.score import CompositeScore
    cs = CompositeScore(
        ticker=ticker, price=1234.5, score=62.0, grade="B", action="BUY",
        technical_score=30.0, momentum_score=16.0, volume_score=10.0, sentiment_score=6.0,
        entry=1234.5, stop_loss=1190.0, target=1320.0, risk_reward=1.9,
        headline=f"{company} trending", narrative="Above both moving averages.\nMore.",
        sector=sector, vix_regime="normal", sector_rank=2, horizon="Swing (3–10 trading days)",
    )
    cs.company_name = company
    return cs


def _fundamentals(symbol):
    from analysis.fundamentals.models import CompanyFundamentals, RatioSnapshot
    return CompanyFundamentals(
        symbol=symbol, provider_name="StubProvider",
        ratios=RatioSnapshot(pe=24.0, pb=3.1, ev_ebitda=12.5, roe=0.16, debt_to_equity=0.42))


_THESIS = SimpleNamespace(
    bull_factors=[{"text": "Price above the 200-day average"}],
    bear_factors=[{"text": "Daily RSI flat for nine sessions"}],
    key_risks=[{"text": "Crude sensitivity"}],
    verdict="Positive", verdict_score=2, verdict_rationale="Trend intact, momentum cooling.",
    notes=[], inputs_present=["composite"],
)


@pytest.fixture
def _stubs(monkeypatch):
    def _blocked(*a, **k):
        raise OSError("network blocked for analyze-stock layout test")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    import streamlit as st
    st.cache_data.clear()   # page-level caches key on ticker; start clean

    import dashboard.shared.cache as cache
    import analysis.thesis as thesis
    import analysis.trend_quality_score as tqs
    import analysis.fundamentals.service as fsvc

    prices = _prices()
    monkeypatch.setattr(cache, "load_ticker_df", lambda t, period="2y": prices.copy())
    monkeypatch.setattr(cache, "_deep_confirmation", lambda t: {
        "weekly": "uptrend", "rel_strength": "leading", "rs_pct": 3.8, "earnings_days": 40,
        "bull": 5, "total": 6, "signals": [("Weekly uptrend", True), ("Volume surge", False)]})
    monkeypatch.setattr(tqs, "score_ticker", lambda *a, **k: SimpleNamespace(
        tqs=64.0, p1=18.0, p2=16.0, p3=15.0, p4=15.0, signal=lambda: "TRENDING"))
    monkeypatch.setattr(thesis, "build_inputs", lambda *a, **k: None)
    monkeypatch.setattr(thesis, "generate_thesis", lambda inputs: _THESIS)

    class _Svc:
        def get_fundamentals(self, ticker):
            return _fundamentals(ticker)
    monkeypatch.setattr(fsvc, "default_service", lambda: _Svc())
    return monkeypatch, cache


def _run(cache, mp, ticker, sector, company):
    mp.setattr(cache, "get_composite_score", lambda t: _score(t, sector, company))
    at = AppTest.from_file(_PAGE, default_timeout=240)
    at.session_state["analyze_ticker"] = ticker
    return at.run()


def _html(at) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def test_overview_layout_non_financial(_stubs):
    mp, cache = _stubs
    at = _run(cache, mp, "TESTCO", "Energy", "Test Energy Ltd")
    assert not at.exception, at.exception
    html = _html(at)

    assert 'class="stock-hd"' in html and "TESTCO" in html and "Test Energy Ltd" in html
    assert [t.label for t in at.tabs][:len(_TABS)] == _TABS
    assert any(b.label == "Size Position" for b in at.button)

    assert "Posture · Test Energy Ltd" in html
    assert html.count('class="hv-pillar"') == 4 and "30 / 40" in html
    assert "Combined read" in html and "Trend quality · TQS" in html and "64 / 90" in html

    assert 'class="thesis-grid"' in html and "Crude sensitivity" in html
    assert "Trend intact, momentum cooling." in html

    assert "Fundamentals · sector-aware" in html
    assert '<dt class="hi">P/E</dt>' in html and "Debt / Equity" in html
    assert "Overlay signals" in html and "4 / 6" not in html and "5 of 6 bullish" in html
    assert "Reference levels" in html and "1 : 1.9" in html

    # Audit Y4: one headline, then every other lens collapsed under "Other lenses".
    blobs = [str(m.value) for m in at.markdown]
    i_hero = next(i for i, b in enumerate(blobs) if "Posture ·" in b)
    i_lens = next(i for i, b in enumerate(blobs) if "Other lenses" in b)
    i_comb = next(i for i, b in enumerate(blobs) if "Combined read" in b)
    assert i_hero < i_lens < i_comb
    assert any("Confidence ·" in str(c.value) and "5 of 6" in str(c.value) for c in at.caption)
    labels = [e.label for e in at.expander]
    assert any("Horizon reads" in x for x in labels) and any("Thesis · bull" in x for x in labels)
    assert not any("gate-by-gate" in x for x in labels)        # no nested expander
    assert any(t.label == "Build the Deep Dive research prompt" for t in at.toggle)

    # Rule 1: the head and hero never print the engine's action string.
    hero = next(str(m.value) for m in at.markdown if "Posture ·" in str(m.value))
    head = next(str(m.value) for m in at.markdown if 'class="stock-hd"' in str(m.value))
    for blob in (hero, head):
        assert "BUY" not in blob and "SELL" not in blob


def test_fundamentals_card_is_sector_aware_for_banks(_stubs):
    mp, cache = _stubs
    at = _run(cache, mp, "TESTBANK", "Banking", "Test Bank Ltd")
    assert not at.exception, at.exception
    card = next(str(m.value) for m in at.markdown if "Fundamentals · sector-aware" in str(m.value))
    assert '<dt class="hi">P/B</dt>' in card and '<dt class="hi">ROE</dt>' in card
    assert '<dt class="hi">P/E</dt>' not in card
    assert "Debt / Equity" not in card and "EV / EBITDA" not in card
    # The Fundamentals tab drops the leverage metric for lenders too (rule 3).
    assert "Debt/Equity" not in _html(at)


def test_fundamentals_tab_keeps_leverage_for_non_financials(_stubs):
    mp, cache = _stubs
    at = _run(cache, mp, "TESTCO2", "Energy", "Test Energy Two Ltd")
    assert not at.exception, at.exception
    assert "Debt/Equity" in _html(at)


def test_deep_dive_toggle_renders_offline(_stubs):
    mp, cache = _stubs
    at = _run(cache, mp, "TESTCO3", "Energy", "Test Energy Three Ltd")
    assert not at.exception, at.exception
    tg = next(t for t in at.toggle if t.label == "Build the Deep Dive research prompt")
    at = tg.set_value(True).run()
    assert not at.exception, at.exception
    assert any("Step 1 · Copy this prompt" in str(m.value) for m in at.markdown)
