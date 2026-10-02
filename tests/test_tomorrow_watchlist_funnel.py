"""
Tomorrow's Watchlist breakout funnel: when the Breakout bucket is empty, the
scan reports how many names survived each breakout gate, in order, so the
page (and the log) can say which gate emptied it.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dashboard.shared import cache as cache_mod  # noqa: E402

# ticker → (action, score, technical, momentum, volume)
_FIXTURE = {
    "PASS.NS":     ("BUY",        60, 30, 10, 6),    # clears every gate → breakout
    "LOWVOL.NS":   ("STRONG BUY", 58, 30, 12, 4),    # dies at volume
    "LOWMOM.NS":   ("BUY",        55, 30, 5,  9),    # dies at momentum
    "LOWSCORE.NS": ("BUY",        48, 30, 12, 9),    # dies at score
    "HOLD.NS":     ("HOLD",       50, 30, 12, 9),    # dies at action
    "EXIT.NS":     ("EXIT",       30, 10, 2,  5),    # breakdown, not BUY-side
    "DEAD.NS":     ("UNAVAILABLE", 0,  0, 0,  0),    # not eligible
}


def _fake_score(tk, **_kw):
    act, sc, tech, mom, vol = _FIXTURE[tk]
    return SimpleNamespace(
        price=100.0, score=sc, grade="B", action=act, headline="h", entry=100.0,
        stop_loss=95.0, target=110.0, risk_reward=2.0, technical_score=tech,
        momentum_score=mom, volume_score=vol, sentiment_score=5, horizon="",
        valid_until="", signals=[], key_levels={},
    )


@pytest.fixture
def run_scan(monkeypatch):
    import analysis.score as score_mod
    import data.universe as uni_mod

    def _run(tickers):
        monkeypatch.setattr(uni_mod, "get_universe", lambda name: list(tickers))
        monkeypatch.setattr(score_mod, "score_stock", _fake_score)
        cache_mod._tomorrow_watchlist.clear()
        try:
            return cache_mod._tomorrow_watchlist(n=15)
        finally:
            cache_mod._tomorrow_watchlist.clear()
    return _run


def test_funnel_counts_each_gate_in_order(run_scan):
    out = run_scan(_FIXTURE)
    assert out["breakout_funnel"] == {
        "eligible": 6,
        "buy_action": 4,        # PASS, LOWVOL, LOWMOM, LOWSCORE
        "score_ge_52": 3,       # minus LOWSCORE
        "momentum_ge_8": 2,     # minus LOWMOM
        "volume_ge_5": 1,       # minus LOWVOL
    }
    # the funnel's last stage agrees with the actual bucket
    assert [p["ticker"] for p in out["breakout_candidates"]] == ["PASS.NS"]


def test_empty_breakout_logs_the_funnel(run_scan, caplog):
    tickers = {k: v for k, v in _FIXTURE.items() if k != "PASS.NS"}
    with caplog.at_level("WARNING"):
        out = run_scan(tickers)
    assert out["breakout_candidates"] == []
    assert out["breakout_funnel"]["volume_ge_5"] == 0
    assert "breakout funnel: eligible=5" in caplog.text
    assert "momentum>=8=1 → volume>=5=0" in caplog.text


def test_each_logged_pick_carries_its_bucket(run_scan):
    out = run_scan(_FIXTURE)
    assert [p["ticker"] for p in out["breakdown_watch"]] == ["EXIT.NS"]
    cache_mod.get_tw_ledger.clear()
    led = cache_mod.get_tw_ledger()
    cache_mod.get_tw_ledger.clear()
    buckets = dict(zip(led["ticker"], led["bucket"]))
    assert buckets["PASS.NS"] == "breakout"
    assert buckets["EXIT.NS"] == "breakdown"
