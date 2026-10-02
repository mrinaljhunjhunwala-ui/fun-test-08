"""Digest picks through the day: opening update, ±2% moves, 15:15 recap."""
from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

import alerts.check_alerts as ca
from analysis.intraday_levels import daily_levels, position_vs_levels, session_stats
from tests.test_alert_copy_no_advice import _assert_descriptive

TODAY = "2026-10-05"
LEVELS = {"y_close": 1600.0, "atr": 60.0, "up": 1630.0, "down": 1570.0, "next_up": 1720.0}


def _daily(n=30, close=1000.0):
    idx = pd.bdate_range(end="2026-10-02", periods=n)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": 1e5}, index=idx)


def _bars(rows, day="2026-10-05"):
    """rows: [(HH:MM, open, high, low, close)] → 5-min bars with a naive IST index."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{day} {t}") for t, *_ in rows])
    return pd.DataFrame([r[1:] for r in rows], index=idx, columns=["Open", "High", "Low", "Close"])


def _at(h, m):
    return datetime.datetime(2026, 10, 5, h, m, tzinfo=ca._IST)


# ── pure helpers ────────────────────────────────────────────────────────────

def test_daily_levels():
    lv = daily_levels(_daily())
    assert lv["y_close"] == 1000.0 and lv["atr"] == pytest.approx(20.0)
    assert (lv["up"], lv["down"], lv["next_up"]) == pytest.approx((1010.0, 990.0, 1040.0))
    assert daily_levels(_daily(10)) is None


def test_session_stats_opening_range_is_first_15_min():
    bars = _bars([("09:15", 1640, 1652, 1630, 1635), ("09:20", 1635, 1640, 1601, 1610),
                  ("09:30", 1610, 1615, 1585, 1600), ("10:00", 1600, 1690, 1598, 1680)])
    ss = session_stats(bars)
    assert ss["open"] == 1640 and ss["last"] == 1680
    assert (ss["low"], ss["high"]) == (1585, 1690)
    assert (ss["or_low"], ss["or_high"]) == (1601, 1652)       # 09:30 bar excluded


@pytest.mark.parametrize("price,text", [
    (1600, "inside its normal range (₹1,570.00–₹1,630.00)"),
    (1650, "above its upside level ₹1,630.00"),
    (1725, "above its next level ₹1,720.00"),
    (1560, "below its downside level ₹1,570.00"),
])
def test_position_vs_levels(price, text):
    assert position_vs_levels(price, LEVELS) == text


# ── alert checks ────────────────────────────────────────────────────────────

@pytest.fixture
def env(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(ca, "dispatch", MagicMock(side_effect=lambda m, **kw: sent.append(m) or True))
    monkeypatch.setattr(ca, "_FORCE_SEND", False)
    kv = {ca._MORNING_PICKS_KEY: {"date": TODAY, "generated_at": "2026-10-05T03:12:00+00:00",
                                  "buys": [{"ticker": "RUBICON.NS", "price": 1600.0}]},
          ca._LEVELS_KEY: {"date": TODAY, "levels": {"RUBICON": LEVELS}}}
    quote = {"RUBICON": {"price": 1600.0, "prev_close": 1600.0, "chg_pct": 0.0}}
    monkeypatch.setitem(sys.modules, "trade_store", SimpleNamespace(
        kv_get=lambda k, d=None, user_id="default": kv.get(k, d)))
    monkeypatch.setitem(sys.modules, "utils.live_price", SimpleNamespace(
        get_live_quote=lambda t: quote.get(t)))
    bars = _bars([("09:15", 1640, 1652, 1630, 1635), ("09:20", 1635, 1640, 1601, 1610),
                  ("10:00", 1610, 1612, 1560, 1565)])
    monkeypatch.setattr("data.fetcher.fetch_intraday", lambda t, interval="5m", days=1: bars)
    return SimpleNamespace(sent=sent, kv=kv, quote=quote)


def test_opening_update_from_0930(env):
    env.quote["RUBICON"] = {"price": 1610.0, "prev_close": 1600.0, "chg_pct": 0.62}
    assert ca.check_opening_update({}, TODAY, now=_at(9, 15)) == 0
    state: dict = {}
    assert ca.check_opening_update(state, TODAY, now=_at(9, 30)) == 1
    msg = env.sent[0]
    assert "RUBICON</b> opened ₹1,640.00 (+2.5% vs yesterday)" in msg
    assert "first 15 min ₹1,601.00–₹1,652.00" in msg
    assert "Now ₹1,610.00 (+0.6%) · inside its normal range" in msg
    _assert_descriptive(msg)
    assert ca.check_opening_update(state, TODAY, now=_at(9, 45)) == 0      # once a day


def test_opening_update_skipped_after_1030(env):
    assert ca.check_opening_update({}, TODAY, now=_at(10, 45)) == 0


def test_move_alerts_fire_once_per_direction(env):
    state: dict = {}
    env.quote["RUBICON"] = {"price": 1565.0, "prev_close": 1600.0, "chg_pct": -2.19}
    assert ca.check_pick_moves(state, TODAY) == 1
    msg = env.sent[-1]
    assert "RUBICON down 2.2% today</b> · ₹1,565.00 (yesterday ₹1,600.00)" in msg
    assert "day range so far ₹1,560.00–₹1,652.00" in msg
    assert "below its downside level ₹1,570.00" in msg
    _assert_descriptive(msg)
    assert ca.check_pick_moves(state, TODAY) == 0                           # still down: no repeat

    env.quote["RUBICON"] = {"price": 1640.0, "prev_close": 1600.0, "chg_pct": 2.5}
    assert ca.check_pick_moves(state, TODAY) == 1                           # the rally is news
    assert "RUBICON up 2.5% today" in env.sent[-1]
    assert len(env.sent) == 2


def test_small_moves_are_quiet(env):
    env.quote["RUBICON"] = {"price": 1630.0, "prev_close": 1600.0, "chg_pct": 1.9}
    assert ca.check_pick_moves({}, TODAY) == 0 and env.sent == []


def test_no_picks_today_means_no_pick_alerts(env):
    env.kv[ca._MORNING_PICKS_KEY]["date"] = "2026-10-02"
    env.quote["RUBICON"] = {"price": 1500.0, "prev_close": 1600.0, "chg_pct": -6.0}
    assert ca.check_pick_moves({}, TODAY) == 0
    assert ca.check_opening_update({}, TODAY, now=_at(9, 30)) == 0
    assert ca.check_picks_recap({}, TODAY, now=_at(15, 20)) == 0
    assert env.sent == []


def test_recap_shows_move_since_scan_day_range_and_swings(env):
    env.quote["RUBICON"] = {"price": 1640.0, "prev_close": 1600.0, "chg_pct": 2.5}
    state = {"mv_down_RUBICON": TODAY, "mv_up_RUBICON": TODAY}
    assert ca.check_picks_recap(state, TODAY, now=_at(15, 0)) == 0
    assert ca.check_picks_recap(state, TODAY, now=_at(15, 15)) == 1
    msg = env.sent[0]
    assert "scan at 08:42 IST" in msg
    assert "RUBICON</b> ₹1,600.00 → <b>₹1,640.00</b> (+2.5% since the scan · +2.50% today)" in msg
    assert "day range ₹1,560.00–₹1,652.00 · up 2%+ at one point · down 2%+ at one point" in msg
    assert "Average +2.5% · 1 up / 0 down or flat" in msg
    _assert_descriptive(msg)
    assert ca.check_picks_recap(state, TODAY, now=_at(15, 30)) == 0


def test_recap_average_never_prints_negative_zero(env):
    env.kv[ca._MORNING_PICKS_KEY]["buys"] = [{"ticker": "A.NS", "price": 100.0},
                                             {"ticker": "B.NS", "price": 100.0}]
    env.quote.update({"A": {"price": 101.0, "chg_pct": 1.0}, "B": {"price": 99.0, "chg_pct": -1.0}})
    assert ca.check_picks_recap({}, TODAY, now=_at(15, 20)) == 1
    assert "Average +0.0%" in env.sent[0]


def test_holdings_never_get_move_alerts(env, monkeypatch):
    monkeypatch.setenv("ALERT_HOLDINGS", "1")
    env.kv["manual_portfolio_holdings"] = [{"ticker": "ONGC.NS", "avg_buy_price": 220}]
    env.quote["ONGC"] = {"price": 200.0, "prev_close": 210.0, "chg_pct": -4.8}
    assert ca.check_pick_moves({}, TODAY) == 0
    assert not any("ONGC" in m for m in env.sent)
