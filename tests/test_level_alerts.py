"""Automatic level alerts: 20-day high/low breaks and 50-day SMA crosses."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import alerts.check_alerts as ca
from analysis.level_alerts import detect_level_events
from tests.test_alert_copy_no_advice import _assert_descriptive

TODAY = "2026-10-01"


def _bars(closes, *, spread=0.01, end="2026-09-30"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame({"Open": closes, "High": closes * (1 + spread),
                         "Low": closes * (1 - spread), "Close": closes,
                         "Volume": np.full(len(closes), 1e5)}, index=idx)


# ── pure detection ──────────────────────────────────────────────────────────

def test_flat_history_inside_range_is_quiet():
    assert detect_level_events(_bars(np.full(60, 100.0)), 100.5) == []


def test_break_above_20_day_high():
    ev = detect_level_events(_bars(np.full(60, 100.0)), 102.0)
    assert [e.kind for e in ev] == ["high20"]
    assert ev[0].level == pytest.approx(101.0)


def test_break_below_20_day_low():
    ev = detect_level_events(_bars(np.full(60, 100.0)), 98.0)
    assert [e.kind for e in ev] == ["low20"]
    assert ev[0].level == pytest.approx(99.0)


def test_cross_above_sma50_from_below():
    # SMA50 of 30×100 + 20×95 = 98: yesterday 95 is below it, today 100 is above.
    ev = detect_level_events(_bars([100.0] * 40 + [95.0] * 20), 100.0)
    assert "sma50_up" in [e.kind for e in ev]


def test_cross_below_sma50_from_above():
    # SMA50 = 102: yesterday 105 is above it, today 100 is below.
    ev = detect_level_events(_bars([100.0] * 40 + [105.0] * 20), 100.0)
    assert "sma50_down" in [e.kind for e in ev]


def test_staying_above_sma50_is_not_a_cross():
    ev = detect_level_events(_bars([100.0] * 40 + [105.0] * 20), 105.5)
    assert not any(e.kind.startswith("sma50") for e in ev)


@pytest.mark.parametrize("prior,price", [
    (_bars(np.full(30, 100.0)), 120.0),     # < 50 sessions
    (pd.DataFrame(), 120.0),
    (_bars(np.full(60, 100.0)), 0.0),
])
def test_insufficient_input_returns_nothing(prior, price):
    assert detect_level_events(prior, price) == []


# ── check_level_alerts ──────────────────────────────────────────────────────

@pytest.fixture
def sent(monkeypatch):
    box: list[str] = []
    monkeypatch.setattr(ca, "dispatch", MagicMock(side_effect=lambda m, **kw: box.append(m) or True))
    monkeypatch.setattr(ca, "_FORCE_SEND", False)
    return box


def _wire(monkeypatch, kv, prices, history):
    monkeypatch.setitem(sys.modules, "trade_store", SimpleNamespace(
        kv_get=lambda k, d=None, user_id="default": kv.get(k, d)))
    monkeypatch.setitem(sys.modules, "utils.live_price", SimpleNamespace(
        get_live_quote=lambda t: {"price": prices[t], "chg_pct": 1.2}))
    monkeypatch.setattr("data.fetcher.fetch_single", lambda t, period="6mo": history[t])


def test_holding_and_digest_pick_breakouts_are_sent_once(sent, monkeypatch):
    # Today's in-progress bar (2026-10-01, a spike) must be ignored for the levels.
    ongc = pd.concat([_bars(np.full(60, 100.0)),
                      _bars([150.0], end="2026-10-01")])
    kv = {"manual_portfolio_holdings": [{"ticker": "ONGC.NS", "avg_buy_price": 90}],
          ca._MORNING_PICKS_KEY: {"date": TODAY, "buys": [{"ticker": "RUBICON.NS"}]}}
    _wire(monkeypatch, kv, {"ONGC": 102.0, "RUBICON": 98.0},
          {"ONGC.NS": ongc, "RUBICON.NS": _bars(np.full(60, 100.0))})

    state: dict = {}
    assert ca.check_level_alerts(state, TODAY) == 2
    assert any("ONGC" in m and "20-day high (₹101.00)" in m and "Portfolio holding" in m for m in sent)
    assert any("RUBICON" in m and "20-day low" in m and "Morning Digest pick" in m for m in sent)
    for m in sent:
        _assert_descriptive(m)

    assert ca.check_level_alerts(state, TODAY) == 0      # deduped for the day
    assert len(sent) == 2


def test_yesterdays_digest_picks_are_not_watched(sent, monkeypatch):
    kv = {ca._MORNING_PICKS_KEY: {"date": "2026-09-30", "buys": [{"ticker": "OLD.NS"}]}}
    _wire(monkeypatch, kv, {}, {})
    assert ca.check_level_alerts({}, TODAY) == 0
    assert sent == []
