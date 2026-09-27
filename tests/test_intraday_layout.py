"""Intraday chart tab (mockup artboard 10): with bars loaded, the tab
renders the toolbar, the six-cell level strip, the descriptive session read
(no trigger / stop / target) and the right rail: depth ladder when Angel
One returns a book, a clean fallback when it does not, and session stats.

fetch_intraday and get_market_depth are stubbed; network is blocked.
"""
import os
import re
import socket

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAGE = os.path.join(_ROOT, "dashboard", "pages", "11_intraday_trader.py")


def _bars(interval="5m", days=3):
    idx = []
    for d in pd.bdate_range("2026-09-22", periods=days):
        idx += list(pd.date_range(d + pd.Timedelta(hours=9, minutes=15),
                                  d + pd.Timedelta(hours=15, minutes=25), freq="5min"))
    idx = pd.DatetimeIndex(idx).tz_localize("Asia/Kolkata")
    rng = np.random.default_rng(7)
    close = 1000 + np.cumsum(rng.normal(0, 1.2, len(idx)))
    return pd.DataFrame({
        "Open": close - 0.3, "High": close + 1.0, "Low": close - 1.0,
        "Close": close, "Volume": rng.integers(1_000, 5_000, len(idx)).astype(float),
    }, index=idx)


_DEPTH = {
    "buys": [{"price": 999.9 - i * 0.1, "qty": 100 * (i + 1), "orders": 3 + i} for i in range(5)],
    "sells": [{"price": 1000.0 + i * 0.1, "qty": 120 * (i + 1), "orders": 4 + i} for i in range(5)],
    "ltp": 1000.0, "total_buy_qty": 1500, "total_sell_qty": 1800, "buy_sell_ratio": 0.83,
}


@pytest.fixture
def _stubs(monkeypatch):
    def _blocked(*a, **k):
        raise OSError("network blocked for intraday layout test")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    import data.fetcher as fetcher
    import data.angel_fetcher as angel
    import streamlit as st
    st.cache_data.clear()  # the 10 s depth cache would leak between tests
    monkeypatch.setattr(fetcher, "fetch_intraday", lambda t, interval="5m", days=3: _bars(interval, days))
    return monkeypatch, angel


def _run(depth):
    at = AppTest.from_file(_PAGE, default_timeout=180)
    at.session_state["ic_last"] = "RELIANCE"
    return at.run()


def _html(at) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def test_chart_tab_layout_with_depth(_stubs):
    mp, angel = _stubs
    mp.setattr(angel, "get_market_depth", lambda t: dict(_DEPTH))
    at = _run(_DEPTH)
    assert not at.exception, at.exception
    html = _html(at)
    assert 'class="chart-tools"' in html and "RELIANCE" in html
    assert len(re.findall(r'class="lc( hi)?"', html)) == 6 and "ORB high" in html and "AVWAP" in html
    assert "Session read · descriptive" in html and "not a trade plan" in html
    card = next(str(m.value) for m in at.markdown if "Session read" in str(m.value))
    for banned in ("trigger", "target", "stop", "aggressive", "breakout", "buy", "sell"):
        assert banned not in card.lower()
    assert html.count('class="depth-row"') == 10 and "spread <b>₹0.10</b>" in html
    assert "Session stats" in html and "Bars today" in html


def test_depth_fallback_without_angel(_stubs):
    mp, angel = _stubs
    mp.setattr(angel, "get_market_depth", lambda t: None)
    at = _run(None)
    assert not at.exception, at.exception
    html = _html(at)
    assert "Market depth needs Angel One" in html
    assert 'class="depth-row"' not in html
