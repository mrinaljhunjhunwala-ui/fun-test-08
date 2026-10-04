"""PIN gate on the Angel One page (FIX AO-PIN, audit 2026-10-04 replica-test S1).

The app has no login, so every viewer could see holdings/funds and place real
orders. The page now stays locked until the owner's PIN is entered — and no
broker call may happen while it is locked.
"""
import os
import socket
import time

import pytest
from streamlit.testing.v1 import AppTest

import data.angel_fetcher as angel
from dashboard.shared import pin_gate as G

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAGE = os.path.join(_ROOT, "dashboard", "pages", "16_angel_one.py")


@pytest.fixture(autouse=True)
def _reset_failures():
    G._FAILS.clear()
    yield
    G._FAILS.clear()


@pytest.fixture
def broker(monkeypatch):
    """Credentials configured; every broker call is recorded."""
    def _blocked(*a, **k):
        raise OSError("network blocked")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    calls = []

    def _rec(name, value):
        def _f(*a, **k):
            calls.append(name)
            return value
        return _f
    monkeypatch.setattr(angel, "is_configured", lambda: True)
    # Sidebar/top-bar live prices go through Angel when it's configured; with
    # the network blocked its instrument-master download retries per symbol
    # and blows the AppTest timeout. Token lookup isn't what's under test.
    monkeypatch.setattr(angel, "_get_token", lambda *a, **k: None)
    now = time.time()
    monkeypatch.setattr(angel, "status", lambda: {
        "configured": True, "logged_in": True, "login_at": now - 600, "renews_in_s": 2400.0,
        "login_breaker_open": False, "login_fails": 0, "data_breaker_open": False,
        "data_fails": 0, "breaker_threshold": 5, "breaker_cooldown_s": 300,
        "candle_calls_last_min": 0, "candle_budget_per_min": 60, "instruments": 0,
        "instruments_loaded_at": None, "tokens_cached": 0, "events": []})
    monkeypatch.setattr(angel, "credential_summary", lambda: {
        k: {"source": "env", "masked": "stored"}
        for k in ("client_id", "api_key", "password", "totp_secret")})
    for name, value in [("_get_session", {"jwt": "x"}), ("get_funds", None),
                        ("get_profile", None), ("get_holdings", []),
                        ("get_positions", {"net": [], "day": []}), ("get_order_book", []),
                        ("get_trade_book", []), ("get_gtt_list", []), ("place_order", None)]:
        monkeypatch.setattr(angel, name, _rec(name, value))
    monkeypatch.delenv("ANGEL_PAGE_PIN", raising=False)
    return monkeypatch, calls


def _account_calls(calls):
    # The sidebar's live-price tier logs in (_get_session) for LTP quotes on
    # every page when Angel is configured; that exposes no account data.
    return [c for c in calls if c != "_get_session"]


def _text(at) -> str:
    return "\n".join(str(e.value) for e in list(at.markdown) + list(at.error) + list(at.caption))


def test_no_pin_configured_fails_closed(broker):
    _, calls = broker
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    assert "is locked" in _text(at) and "page_pin" in "\n".join(c.value for c in at.code)
    assert not at.tabs and _account_calls(calls) == []


def test_locked_page_makes_no_broker_call(broker):
    mp, calls = broker
    mp.setenv("ANGEL_PAGE_PIN", "4821")
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    assert "PIN-protected" in _text(at)
    assert not at.tabs and _account_calls(calls) == []


def test_wrong_pin_stays_locked_right_pin_unlocks(broker):
    mp, calls = broker
    mp.setenv("ANGEL_PAGE_PIN", "4821")
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    at.text_input(key="pin_input_angel_one").input("0000")
    at.button[0].click().run()
    assert "Wrong PIN" in _text(at) and not at.tabs and _account_calls(calls) == []

    at.text_input(key="pin_input_angel_one").input("4821")
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert "Quick order" in [t.label for t in at.tabs]


def test_check_pin_locks_out_after_max_attempts():
    t0 = 1_000.0
    for _ in range(G.MAX_ATTEMPTS - 1):
        assert G.check_pin("s", "bad", "4821", now=t0) == "wrong"
    assert G.check_pin("s", "bad", "4821", now=t0) == "locked"
    # Even the right PIN is refused during the lockout…
    assert G.check_pin("s", "4821", "4821", now=t0 + 10) == "locked"
    # …and accepted once it expires.
    assert G.check_pin("s", "4821", "4821", now=t0 + G.LOCKOUT_S + 1) == "ok"


def test_unlock_expires(monkeypatch):
    state = {}
    monkeypatch.setattr(G.st, "session_state", state)
    state["_pin_unlocked_at_angel_one"] = time.time() - G.UNLOCK_TTL_S - 1
    assert not G.is_unlocked("angel_one")
    state["_pin_unlocked_at_angel_one"] = time.time()
    assert G.is_unlocked("angel_one")
