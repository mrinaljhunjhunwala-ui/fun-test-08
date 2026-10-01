"""tests/test_alerts_portfolio.py — holdings-driven portfolio alerts.

check_portfolio_alerts reads the My Portfolio holdings from the KV store on
every run, so alerts follow the portfolio with no list to maintain. Offline:
the store, the quote source and dispatch are all faked.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

import alerts.check_alerts as ca

TODAY = "2026-09-29"


class _FakeStore:
    def __init__(self, holdings, anchors=None):
        self.kv = {("manual_portfolio_holdings", "default"): holdings,
                   ("portfolio_alert_anchors", "_system"): anchors}

    def kv_get(self, key, default=None, user_id="default"):
        v = self.kv.get((key, user_id))
        return default if v is None else v

    def kv_set(self, key, value, user_id="default"):
        self.kv[(key, user_id)] = value
        return True

    @property
    def anchors(self):
        return self.kv[("portfolio_alert_anchors", "_system")]


@pytest.fixture
def env(monkeypatch):
    """Returns (install, prices, dispatch). install(holdings, anchors) wires a fake store."""
    prices: dict = {}
    fake_lp = types.SimpleNamespace(
        get_live_quote=lambda t: {"price": prices[t], "chg_pct": 0.0} if t in prices else None)
    monkeypatch.setitem(sys.modules, "utils.live_price", fake_lp)
    dispatch = MagicMock(return_value=True)
    monkeypatch.setattr(ca, "dispatch", dispatch)
    monkeypatch.setattr(ca, "_FORCE_SEND", False)

    def install(holdings, anchors=None):
        store = _FakeStore(holdings, anchors)
        monkeypatch.setitem(sys.modules, "trade_store", store)
        return store
    return install, prices, dispatch


def _h(ticker, avg):
    return {"ticker": f"{ticker}.NS", "quantity": 10, "avg_buy_price": avg, "date_bought": "2026-01-01"}


def test_first_sighting_sets_reference_without_alerting(env):
    install, prices, dispatch = env
    store = install([_h("ONGC", 223.97)])
    prices["ONGC"] = 230.0
    assert ca.check_portfolio_alerts({}, TODAY) == 0
    dispatch.assert_not_called()
    assert store.anchors["ONGC"] == {"ref": 230.0, "ref_date": TODAY, "side": "above"}


def test_step_move_alerts_and_reanchors(env):
    install, prices, dispatch = env
    store = install([_h("PFC", 423.15)], {"PFC": {"ref": 330.9, "ref_date": "2026-09-28", "side": "below"}})
    prices["PFC"] = 364.5                       # +10.2%
    state = {}
    assert ca.check_portfolio_alerts(state, TODAY) == 1
    assert "+10.2%" in dispatch.call_args[0][0]
    assert store.anchors["PFC"]["ref"] == 364.5  # next alert is the NEXT 10% move
    # same price again: no repeat
    dispatch.reset_mock()
    assert ca.check_portfolio_alerts(state, TODAY) == 0
    dispatch.assert_not_called()


def test_small_move_is_quiet(env):
    install, prices, dispatch = env
    install([_h("PFC", 423.15)], {"PFC": {"ref": 330.9, "ref_date": "2026-09-28", "side": "below"}})
    prices["PFC"] = 340.0                       # +2.7%
    assert ca.check_portfolio_alerts({}, TODAY) == 0
    dispatch.assert_not_called()


def test_cost_cross_alerts_once_per_crossing(env):
    install, prices, dispatch = env
    store = install([_h("VEDL", 253.22)], {"VEDL": {"ref": 259.95, "ref_date": "2026-09-28", "side": "above"}})
    prices["VEDL"] = 250.0                      # 1.3% below cost, only -4% from ref
    assert ca.check_portfolio_alerts({}, TODAY) == 1
    assert "crossed below your average cost" in dispatch.call_args[0][0]
    assert store.anchors["VEDL"]["side"] == "below"
    dispatch.reset_mock()
    assert ca.check_portfolio_alerts({}, TODAY) == 0   # still below: no repeat


def test_avg_price_edit_on_portfolio_page_is_picked_up(env):
    install, prices, dispatch = env
    # Holding re-averaged to 260 on the My Portfolio page; price 257 is now >1% below cost.
    install([_h("VEDL", 260.0)], {"VEDL": {"ref": 259.95, "ref_date": "2026-09-28", "side": "above"}})
    prices["VEDL"] = 257.0
    assert ca.check_portfolio_alerts({}, TODAY) == 1
    assert "₹260.00" in dispatch.call_args[0][0]


def test_new_and_sold_holdings(env):
    install, prices, dispatch = env
    store = install([_h("COALINDIA", 438.05)],
                    {"XCHANGING": {"ref": 60.04, "ref_date": "2026-09-28", "side": "below"}})
    prices["COALINDIA"] = 423.25
    ca.check_portfolio_alerts({}, TODAY)
    assert "XCHANGING" not in store.anchors     # sold: reference dropped
    assert store.anchors["COALINDIA"]["ref"] == 423.25   # bought: tracked from now


def test_no_holdings_or_no_quote_degrades_quietly(env):
    install, prices, dispatch = env
    install([])
    assert ca.check_portfolio_alerts({}, TODAY) == 0
    store = install([_h("VISL", 20.0)], {"VISL": {"ref": 31.35, "ref_date": "2026-09-28", "side": "above"}})
    assert ca.check_portfolio_alerts({}, TODAY) == 0      # no quote for VISL
    assert store.anchors["VISL"]["ref"] == 31.35          # untouched, not dropped
    dispatch.assert_not_called()


def test_wobble_inside_cost_margin_is_quiet(env):
    # 2026-10-01: ONGC at 224.12 / 223.91 around a 223.97 average sent two alerts.
    install, prices, dispatch = env
    store = install([_h("ONGC", 223.97)], {"ONGC": {"ref": 225.0, "ref_date": "2026-09-30", "side": "above"}})
    for p in (223.91, 224.12, 222.0, 225.5):        # all within ±1% (221.73–226.21)
        prices["ONGC"] = p
        assert ca.check_portfolio_alerts({}, TODAY) == 0
    dispatch.assert_not_called()
    assert store.anchors["ONGC"]["side"] == "above"


def test_one_cost_cross_alert_per_holding_per_day(env):
    install, prices, dispatch = env
    store = install([_h("VEDL", 253.22)], {"VEDL": {"ref": 255.0, "ref_date": "2026-09-30", "side": "above"}})
    state: dict = {}
    prices["VEDL"] = 250.0                          # >1% below → alert
    assert ca.check_portfolio_alerts(state, TODAY) == 1
    prices["VEDL"] = 256.5                          # >1% above, same day → silent
    assert ca.check_portfolio_alerts(state, TODAY) == 0
    assert dispatch.call_count == 1
    assert store.anchors["VEDL"]["side"] == "above"  # side still tracked for tomorrow
