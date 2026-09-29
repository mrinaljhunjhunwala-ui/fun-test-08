"""Angel One (mockup artboard 11): the status panel, masked credentials and
activity log render in both states; the connected page adds the broker tabs;
holding-posture codes show as descriptive labels; credential masking never
returns a secret.

angel_fetcher's broker calls are stubbed; network is blocked.
"""
import os
import socket
import time
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

import data.angel_fetcher as angel

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAGE = os.path.join(_ROOT, "dashboard", "pages", "16_angel_one.py")

_SECRET_KEY = "sk_live_abcdefgh7f2c"
_SECRET_PW = "hunter2-password"
_SECRET_TOTP = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def _net(monkeypatch):
    def _blocked(*a, **k):
        raise OSError("network blocked for angel one layout test")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    for env in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"):
        monkeypatch.delenv(env, raising=False)
    return monkeypatch


def _html(at) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def test_not_connected_state(_net):
    _net.setattr(angel, "is_configured", lambda: False)
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    html = _html(at)
    assert 'class="ao-status"' in html and "Not connected" in html
    assert html.count(">missing<") == 4
    assert "Connect in four steps" in html
    assert 'class="ao-stat"' not in html          # no all-dash tiles before setup
    assert not at.tabs


def _connected(mp):
    now = time.time()
    mp.setattr(angel, "is_configured", lambda: True)
    mp.setattr(angel, "_get_session", lambda: {"jwt": "x"})
    mp.setattr(angel, "status", lambda: {
        "configured": True, "logged_in": True, "login_at": now - 600, "renews_in_s": 2400.0,
        "login_breaker_open": False, "login_fails": 0, "data_breaker_open": False,
        "data_fails": 1, "breaker_threshold": 5, "breaker_cooldown_s": 300,
        "candle_calls_last_min": 12, "candle_budget_per_min": 60,
        "instruments": 8421, "instruments_loaded_at": now - 900, "tokens_cached": 247,
        "events": [{"ts": now - 600, "kind": "Session", "detail": "logged in · TOTP verified",
                    "tone": "good"}],
    })
    mp.setattr(angel, "credential_summary", lambda: {
        "client_id": {"source": "secrets", "masked": "A••••4213"},
        "api_key": {"source": "secrets", "masked": "••••7f2c"},
        "password": {"source": "secrets", "masked": "stored"},
        "totp_secret": {"source": "secrets", "masked": "stored"},
    })
    mp.setattr(angel, "get_funds", lambda: {"available_cash": 125000.0, "used_margin": 5000.0,
                                            "m2m": -1200.0})
    mp.setattr(angel, "get_profile", lambda: {"name": "Test User", "client_id": "A1234213",
                                              "email": "t@example.com", "exchanges": ["NSE", "BSE"]})
    mp.setattr(angel, "get_holdings", lambda: [
        {"symbol": "RELIANCE", "qty": 10, "avg_price": 1100.0, "ltp": 1182.0,
         "pnl": 820.0, "pnl_pct": 7.45, "value_rs": 11820.0}])
    mp.setattr(angel, "get_positions", lambda: {"net": [], "day": []})
    mp.setattr(angel, "get_order_book", lambda: [])
    mp.setattr(angel, "get_trade_book", lambda: [])
    mp.setattr(angel, "get_gtt_list", lambda: [])


def test_connected_state(_net):
    _connected(_net)
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    html = _html(at)
    assert "Connected · authenticated" in html and "renews at" in html
    assert html.count('class="ao-stat"') == 6 and "12 / 60" in html and "8,421" in html
    assert "A••••4213" in html and "••••7f2c" in html and ">missing<" not in html
    assert "logged in · TOTP verified" in html
    labels = [t.label for t in at.tabs]   # includes the nested order-book tabs
    for want in ("Account", "Holdings", "Today's positions", "Orders & trades",
                 "Quick order", "Holding posture"):
        assert want in labels
    assert "₹1,25,000" in html or "₹125,000" in html
    assert "Total P&amp;L" in html or "Total P&L" in html


def test_posture_codes_render_as_descriptive_labels(_net):
    _connected(_net)
    import analysis.portfolio_posture as pp
    import data.fetcher as fetcher

    def _h(sym, posture):
        return SimpleNamespace(symbol=sym, posture=posture, filters_passing=2, pnl_pct=-4.0,
                               reason="below 50-day average", rsi=41, sma50=100.0, atr=3.0,
                               suggested_stop=95.0, rs_63d=-6.0)
    _net.setattr(pp, "analyse_holdings", lambda *a, **k: [_h("AAA", "EXIT_WATCH"),
                                                          _h("BBB", "ADD_WATCH")])
    _net.setattr(pp, "analyse_positions", lambda *a, **k: [])
    _net.setattr(pp, "format_portfolio_message", lambda h, i: "msg")
    _net.setattr(fetcher, "fetch_single", lambda *a, **k: None)
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    next(b for b in at.button if b.label == "Run posture analysis").click().run()
    assert not at.exception, at.exception
    cards = "\n".join(str(m.value) for m in at.markdown if "filters · P&amp;L" in str(m.value)
                      or "filters · P&L" in str(m.value))
    assert "Trend broken" in cards and "Strengthening" in cards
    assert "EXIT" not in cards and "ADD" not in cards and "Suggested stop" not in cards


def test_credential_masking_never_returns_secrets(monkeypatch):
    monkeypatch.setenv("ANGEL_CLIENT_ID", "A1234213")
    monkeypatch.setenv("ANGEL_API_KEY", _SECRET_KEY)
    monkeypatch.setenv("ANGEL_PASSWORD", _SECRET_PW)
    monkeypatch.setenv("ANGEL_TOTP_SECRET", _SECRET_TOTP)
    out = angel.credential_summary()
    assert out["client_id"]["masked"] == "A••••4213"
    assert out["api_key"]["masked"] == "••••7f2c"
    assert out["password"]["masked"] == "stored" and out["totp_secret"]["masked"] == "stored"
    blob = repr(out)
    for secret in (_SECRET_KEY, _SECRET_PW, _SECRET_TOTP, "abcdefgh"):
        assert secret not in blob
    monkeypatch.delenv("ANGEL_PASSWORD")
    assert angel.credential_summary()["password"] == {"source": "", "masked": ""}


def test_status_snapshot_and_event_log(monkeypatch):
    monkeypatch.setattr(angel, "_SESSION", {"jwt": None, "feed_token": None, "api_key": "", "ts": 0.0})
    angel.clear_session()
    s = angel.status()
    assert s["logged_in"] is False and s["renews_in_s"] is None
    assert s["events"][0]["kind"] == "Session" and "cleared" in s["events"][0]["detail"]
    assert 0 <= s["candle_calls_last_min"] <= s["candle_budget_per_min"]
