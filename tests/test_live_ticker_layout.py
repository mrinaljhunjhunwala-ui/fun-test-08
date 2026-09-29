"""Live Ticker (mockup artboard 06): the page renders the dense price table
(LTP, change, volume, day range, VWAP), the index rail with sparklines and
the feed card, then the market-wide breadth strip. A row flashes once when
its price changed since the previous render, and only then.

Quotes, the index rail, market status and news are stubbed; network is
blocked.
"""
import os
import socket

import pytest
from streamlit.testing.v1 import AppTest

from dashboard.shared.ui_components import fmt_volume, sparkline, ticker_table
from utils.live_price import _normalise, _vwap_from_bars

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAGE = os.path.join(_ROOT, "dashboard", "pages", "01_market_live.py")

_PRICES = {"bump": 0.0}


def _quote(t, i):
    px = 1000.0 + i * 7 + (_PRICES["bump"] if t == "RELIANCE.NS" else 0.0)
    prev = 1000.0 + i * 5
    return {"price": px, "prev_close": prev, "chg_pct": (px / prev - 1) * 100,
            "volume": 2_420_000 + i, "high": px + 5, "low": prev - 5, "vwap": px - 1}


@pytest.fixture
def _stubs(monkeypatch):
    def _blocked(*a, **k):
        raise OSError("network blocked for live ticker layout test")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    import streamlit as st
    st.cache_data.clear()
    _PRICES["bump"] = 0.0

    import utils.live_price as lp
    import utils.market_hours as mh
    import utils.news as news
    import data.angel_fetcher as angel
    import dashboard.shared.chart_helpers as ch

    monkeypatch.setattr(angel, "is_configured", lambda: False)
    monkeypatch.setattr(lp, "get_live_prices_batch",
                        lambda syms, **k: {t: _quote(t, i) for i, t in enumerate(syms)})
    monkeypatch.setattr(mh, "market_status", lambda: {
        "is_open": False, "is_pre": False, "status": "Closed", "color": "🔴",
        "detail": "Next open: Wed 30 Sep, 9:15 AM IST", "time_ist": "20:47 IST",
        "day": "Tuesday, 29 Sep 2026"})
    monkeypatch.setattr(ch, "index_rail_data", lambda: [
        {"label": "Nifty 50", "price": 22716.0, "chg": -410.0, "chg_pct": -1.77,
         "spark": [23100, 23000, 22900, 22716]},
        {"label": "India VIX", "price": 13.41, "chg": 1.25, "chg_pct": 10.3,
         "spark": [12.1, 12.6, 13.0, 13.41]},
    ])
    monkeypatch.setattr(news, "get_market_news", lambda **k: [])


def _html(at) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def test_live_ticker_layout(_stubs):
    at = AppTest.from_file(_PAGE, default_timeout=180).run()
    assert not at.exception, at.exception
    html = _html(at)
    table = next(str(m.value) for m in at.markdown if 'class="ticker-table"' in str(m.value))
    assert table.count("<tr>") + table.count("<tr ") == 51          # header + Nifty 50
    for col in ("LTP", "Volume", "Day high", "Day low", "VWAP"):
        assert f"<th>{col}</th>" in table
    assert "24.2L" in table                                         # Indian volume units
    assert "tk-flash" not in table                                  # first sight: no flash
    assert html.count('class="mini-index"') == 2 and 'class="mini-spark"' in html
    assert "Feed" in html and "50 / 50" in html
    assert "Market breadth" in html and "Advances / declines" in html
    assert 'class="ml-tape"' not in html and "Trade Ideas" not in html


def test_row_flashes_only_when_price_ticks(_stubs):
    import streamlit as st
    at = AppTest.from_file(_PAGE, default_timeout=180).run()
    _PRICES["bump"] = 3.0
    st.cache_data.clear()
    at.run()
    assert not at.exception, at.exception
    table = next(str(m.value) for m in at.markdown if 'class="ticker-table"' in str(m.value))
    assert table.count("tk-flash-up") == 1 and "tk-flash-dn" not in table
    flashed = table.split('class="tk-flash-up"')[1].split("</tr>")[0]
    assert "RELIANCE" in flashed


def test_helpers():
    assert fmt_volume(24_200_000) == "2.42Cr"
    assert fmt_volume(8_100_000) == "81L" and fmt_volume(480_000) == "4.8L"
    assert fmt_volume(None) == "—" and fmt_volume(0) == "—"
    assert "var(--bull)" in sparkline([1, 2, 3]) and "var(--bear)" in sparkline([3, 2, 1])
    assert sparkline([5]) == ""
    row = ticker_table([{"sym": "ABC", "sub": "IT", "price": 101.0, "chg": 1.0, "chg_pct": 1.0,
                         "volume": None, "high": None, "low": None, "vwap": 100.0}])
    assert "—" in row and '<span class="tk-up">100.00</span>' in row   # price above VWAP


def test_vwap_from_bars_and_passthrough():
    res = {"indicators": {"quote": [{
        "high": [11.0, 12.0, None], "low": [9.0, 10.0, 1.0],
        "close": [10.0, 11.0, 1.0], "volume": [100, 300, 50]}]}}
    # typical prices 10 and 11, weights 100 and 300; the None bar is skipped
    assert _vwap_from_bars(res) == {"vwap": pytest.approx(10.75)}
    assert _vwap_from_bars({}) == {}
    out = _normalise({"price": 11.0, "prev_close": 10.0, "high": 11.5, "low": 9.8, "vwap": 10.7})
    assert out["high"] == 11.5 and out["low"] == 9.8 and out["vwap"] == 10.7
    assert "vwap" not in _normalise({"price": 11.0, "prev_close": 10.0})
