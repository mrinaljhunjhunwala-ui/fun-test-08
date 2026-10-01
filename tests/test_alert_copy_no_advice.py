"""CLAUDE.md rule 1 guard for Telegram/Gmail alert copy.

Drives every message-producing path in alerts/check_alerts.py (plus the
formatters it calls in analysis/momentum_scanner.py and
analysis/portfolio_posture.py) with offline fixtures, captures what would be
dispatched, and asserts none of it reads as an instruction. Messages must
describe market state ("Nifty is below SMA20 and SMA50"), never tell the
reader what to do ("be defensive with new buys").

Internal engine labels that are data, not copy (EXIT_WATCH / TRIM_WATCH /
ADD_WATCH / STOP_HIT posture tags), are stripped before matching.
"""
from __future__ import annotations

import datetime
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import alerts.check_alerts as ca
from analysis.momentum_scanner import format_momentum_message
from analysis.portfolio_posture import (
    HoldingPosture, _classify_delivery, analyse_intraday_position,
    format_portfolio_message,
)

ADVISORY = re.compile(
    r"\b(buy|buys|buying|sell|sells|selling|avoid|tighten|protect|"
    r"be defensive|defensive with|should|consider|sit on hands|"
    r"wait \d+|before (entering|any action)|enter|entering|"
    r"size positions|go (long|short)|book profits?|trim|add more)\b",
    re.I,
)
_POSTURE_TAGS = re.compile(r"\[[A-Z_]+\]")


def _assert_descriptive(msg: str) -> None:
    text = _POSTURE_TAGS.sub("", re.sub(r"<[^>]+>", "", msg))
    m = ADVISORY.search(text)
    assert not m, f"Advisory phrase {m.group(0)!r} in alert:\n{msg}"


@pytest.fixture
def sent(monkeypatch):
    """Capture every dispatched message; never send."""
    box: list[str] = []
    monkeypatch.setattr(ca, "dispatch", MagicMock(side_effect=lambda m, **kw: box.append(m) or True))
    monkeypatch.setattr(ca, "_FORCE_SEND", False)
    return box


def _snapshot(actions=("STRONG BUY", "BUY", "WATCHLIST")):
    buys = [{
        "ticker": f"TICK{i}", "action": a, "score": 85 - i * 10,
        "price": 1000.0 + i, "entry_zone": "~1000", "stop_loss": 950,
        "target": 1150, "sector": "IT",
    } for i, a in enumerate(actions)]
    return {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "data": {"buys": buys, "sells": [], "meta": {}}}


def _ohlcv(closes):
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range("2026-06-01", periods=len(closes), freq="B")
    return pd.DataFrame({"Open": closes, "High": closes * 1.01, "Low": closes * 0.99,
                         "Close": closes, "Volume": np.full(len(closes), 1e5)}, index=idx)


# ─────────────────────────────────────────────────────────────────────────────

def test_vix_regime_alert_is_descriptive(sent, monkeypatch):
    for regime in ("fear", "panic"):
        monkeypatch.setattr("utils.vix.get_india_vix_regime",
                            lambda r=regime: {"regime": r, "vix": 27.4})
        assert ca.check_vix_regime({}, "2026-09-30") == 1
    assert len(sent) == 2
    for m in sent:
        _assert_descriptive(m)
    assert "27.4" in sent[0] and "FEAR" in sent[0]


def test_nifty_trend_alert_is_descriptive(sent, monkeypatch):
    falling = np.linspace(26000, 23000, 70)       # price < SMA20 < SMA50
    monkeypatch.setattr("data.fetcher.fetch_single", lambda *a, **kw: _ohlcv(falling))
    assert ca.check_nifty_trend({}, "2026-09-30") == 1
    _assert_descriptive(sent[0])
    assert "SMA20" in sent[0] and "SMA50" in sent[0] and "downtrend" in sent[0]


def test_price_and_portfolio_alerts_are_descriptive(sent, monkeypatch, tmp_path):
    csv = tmp_path / "alerts.csv"
    csv.write_text("ticker,condition,level,note,enabled\nTCS,above,3000,,1\n", encoding="utf-8")
    monkeypatch.setattr(ca, "_ALERTS_CSV", str(csv))
    prices = {"TCS": 3100.0, "VEDL": 250.0, "PFC": 400.0}
    monkeypatch.setitem(sys.modules, "utils.live_price", SimpleNamespace(
        get_live_quote=lambda t: {"price": prices[t], "chg_pct": 1.0}))
    kv = {"manual_portfolio_holdings": [
              {"ticker": "VEDL.NS", "avg_buy_price": 253.22},
              {"ticker": "PFC.NS", "avg_buy_price": 300.0}],
          "portfolio_alert_anchors": {
              "VEDL": {"ref": 259.95, "ref_date": "2026-09-28", "side": "above"},
              "PFC": {"ref": 330.0, "ref_date": "2026-09-28", "side": "above"}}}
    monkeypatch.setitem(sys.modules, "trade_store", SimpleNamespace(
        kv_get=lambda k, d=None, user_id="default": kv.get(k, d),
        kv_set=lambda k, v, user_id="default": kv.__setitem__(k, v)))

    assert ca.check_price_alerts({}, "2026-09-30") == 1
    assert ca.check_portfolio_alerts({}, "2026-09-30") == 2   # VEDL cost cross + PFC +21% step
    assert len(sent) == 3
    for m in sent:
        _assert_descriptive(m)


def test_delivery_digest_is_descriptive_and_maps_action_labels(sent, monkeypatch):
    monkeypatch.setitem(sys.modules, "trade_store",
                        SimpleNamespace(kv_get=lambda k, user_id: _snapshot()))
    assert ca.check_delivery_digest({}, "2026-09-30") == 1
    _assert_descriptive(sent[0])
    assert "Strong Trend" in sent[0] and "Uptrend" in sent[0]
    assert "Score 85/90" in sent[0]                            # data unchanged


def test_intraday_watchlist_is_descriptive(sent, monkeypatch):
    monkeypatch.setitem(sys.modules, "trade_store",
                        SimpleNamespace(kv_get=lambda k, user_id: _snapshot()))
    monkeypatch.setattr("data.fetcher.fetch_single", lambda *a, **kw: _ohlcv(np.full(30, 1000.0)))
    assert ca.check_intraday_watchlist({}, "2026-09-30") == 1
    _assert_descriptive(sent[0])


def test_weekly_digest_is_descriptive(sent, monkeypatch):
    _real_strptime = datetime.datetime.strptime
    fake_now = datetime.datetime(2026, 9, 27, 9, 0, tzinfo=ca._IST)   # Sunday

    class _FakeDt(datetime.datetime):
        @classmethod
        def now(cls, tz=None): return fake_now

        @classmethod
        def strptime(cls, s, f): return _real_strptime(s, f)
    monkeypatch.setattr(ca.datetime, "datetime", _FakeDt)
    monkeypatch.setitem(sys.modules, "trade_store",
                        SimpleNamespace(kv_get=lambda k, user_id: _snapshot()))
    monkeypatch.setattr("alerts.journal_store.read_entries", lambda: [])
    assert ca.check_weekly_digest({}, "2026-09-27") == 1
    _assert_descriptive(sent[0])
    assert "Strong Trend" in sent[0]


def test_momentum_messages_are_descriptive():
    cand = {"ticker": "ABC", "price": 500.0, "breakout_pct": 2.1, "volume_ratio": 1.8,
            "rsi": 64.0, "rs_63d": 7.5, "suggested_entry": 500.0,
            "suggested_stop": 480.0, "suggested_target": 540.0}
    _assert_descriptive(format_momentum_message([]))
    _assert_descriptive(format_momentum_message([cand]))


def test_portfolio_posture_messages_are_descriptive():
    """Uses the engine's real reason strings for every delivery + intraday posture."""
    keys = ("trend", "breakout", "volume", "rsi_band", "vwap", "atr_ok")
    holdings = []
    for n_pass, ltp, pnl in ((1, 90.0, -10.0), (6, 120.0, 20.0), (2, 110.0, 5.0), (4, 110.0, 5.0)):
        filters = {k: i < n_pass for i, k in enumerate(keys)}
        posture, reason = _classify_delivery(filters, pnl, ltp, 100.0, 3.0)
        holdings.append(HoldingPosture(symbol="XYZ", qty=10, avg_price=100.0, ltp=ltp,
                                       pnl_pct=pnl, posture=posture, reason=reason,
                                       filters_passing=n_pass))
    positions = [
        analyse_intraday_position({"symbol": "ABC", "qty": 5, "avg_price": 100.0,
                                   "ltp": ltp, "side": "LONG"}, _ohlcv(np.full(30, 100.0)))
        for ltp in (98.0, 103.0, 101.5, 100.2)          # STOP / TARGET / TRAIL / RUNNING
    ]
    assert {p.posture for p in positions} >= {"STOP_HIT", "TARGET_HIT", "TRAIL_TIGHTER"}
    _assert_descriptive(format_portfolio_message([], []))
    _assert_descriptive(format_portfolio_message(holdings, positions, only_actionable=False))


def test_guard_catches_the_old_copy():
    """The regex must actually flag the phrasings this suite was written to remove."""
    for old in ("Trend has turned down — be defensive with new buys.",
                "New long entries are higher-risk — protect open positions, "
                "tighten stops, avoid fresh leverage.",
                "Quiet day for new positional entries — sit on hands.",
                "Wait 15 min after open before entering",
                "crossed below your avg buy price"):
        assert ADVISORY.search(old), old
