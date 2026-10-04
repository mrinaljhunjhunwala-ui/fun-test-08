"""
FIX W-SPEED regression tests — get_tomorrow_watchlist() snapshot wrapper in
dashboard/shared/cache.py, mirroring the existing get_top_picks() pattern
(FIX SPEED1). Also covers the FIX HZ1-WL horizon-field threading fix.

Run:  py -m pytest tests/test_tomorrow_watchlist_snapshot.py -q
"""
import datetime
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def test_fresh_snapshot_is_used_without_live_scan(monkeypatch):
    """A recent, well-formed snapshot must be returned as-is — the expensive
    live scan (_tomorrow_watchlist) must NOT be called at all."""
    import dashboard.shared.cache as cache_mod

    fresh_data = {"breakout_candidates": [{"ticker": "FAKE.NS"}],
                  "breakdown_watch": [], "reversal_watch": [], "scan_time": "20 Jul 15:50"}
    snapshot = {
        "data": fresh_data,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scan_seconds": 42.0,
    }
    monkeypatch.setattr(cache_mod._store, "kv_get",
                        lambda key, user_id=None: snapshot)

    def _boom(n=15):
        raise AssertionError("_tomorrow_watchlist should not be called when snapshot is fresh")

    monkeypatch.setattr(cache_mod, "_tomorrow_watchlist", _boom)

    result = cache_mod.get_tomorrow_watchlist()
    assert result["source"] == "persisted"
    assert result["breakout_candidates"] == [{"ticker": "FAKE.NS"}]


def test_stale_snapshot_falls_back_to_live_scan(monkeypatch):
    """A snapshot from an earlier session must NOT be used — falls back to a
    live scan instead."""
    import dashboard.shared.cache as cache_mod

    old_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)
    snapshot = {
        "data": {"breakout_candidates": [], "breakdown_watch": [],
                 "reversal_watch": [], "scan_time": "yesterday"},
        "generated_at": old_time.isoformat(),
    }
    monkeypatch.setattr(cache_mod._store, "kv_get",
                        lambda key, user_id=None: snapshot)

    live_result = {"breakout_candidates": [], "breakdown_watch": [],
                  "reversal_watch": [], "scan_time": "just now"}
    monkeypatch.setattr(cache_mod, "_tomorrow_watchlist", lambda n=15: live_result)

    result = cache_mod.get_tomorrow_watchlist()
    assert result["source"] == "live_scan"
    assert result["scan_time"] == "just now"


def test_missing_snapshot_falls_back_to_live_scan(monkeypatch):
    """No snapshot at all (first-ever deploy) — must degrade to a live scan,
    not raise."""
    import dashboard.shared.cache as cache_mod

    monkeypatch.setattr(cache_mod._store, "kv_get", lambda key, user_id=None: None)
    live_result = {"breakout_candidates": [], "breakdown_watch": [],
                  "reversal_watch": [], "scan_time": "fresh scan"}
    monkeypatch.setattr(cache_mod, "_tomorrow_watchlist", lambda n=15: live_result)

    result = cache_mod.get_tomorrow_watchlist()
    assert result["source"] == "live_scan"


def test_kv_get_exception_falls_back_to_live_scan(monkeypatch):
    """A broken/unreachable trade_store must degrade gracefully, not raise."""
    import dashboard.shared.cache as cache_mod

    def _boom(key, user_id=None):
        raise ConnectionError("simulated trade_store outage")

    monkeypatch.setattr(cache_mod._store, "kv_get", _boom)
    live_result = {"breakout_candidates": [], "breakdown_watch": [],
                  "reversal_watch": [], "scan_time": "fresh scan"}
    monkeypatch.setattr(cache_mod, "_tomorrow_watchlist", lambda n=15: live_result)

    result = cache_mod.get_tomorrow_watchlist()
    assert result["source"] == "live_scan"


# ───────────────────────── FIX HZ1-WL: horizon threading ─────────────────────

def test_tomorrow_watchlist_threads_horizon_through(monkeypatch):
    """score_stock() already computes a horizon label + valid_until for every
    stock; _tomorrow_watchlist() was discarding both before they reached the
    output dict. Confirm they now survive into the bucketed output."""
    import dashboard.shared.cache as cache_mod
    from analysis.score import CompositeScore
    import data.universe as universe_mod

    # test_pages_smoke.py executes every dashboard page headlessly (network
    # blocked), including this one's top-level probe-thread call to
    # _tomorrow_watchlist(n=15) — which populates st.cache_data's cache with
    # a real (empty, network-blocked) result under that same (n=15) key.
    # Without clearing it here, this call would silently return that stale
    # cached entry instead of re-executing with the monkeypatches below.
    cache_mod._tomorrow_watchlist.clear()

    monkeypatch.setattr(universe_mod, "get_universe", lambda key: ["FAKE1.NS"])

    fake_score = CompositeScore(
        ticker="FAKE1.NS", price=100.0, score=70.0, grade="B", action="BUY",
        technical_score=30.0, momentum_score=15.0, volume_score=10.0,
        sentiment_score=5.0, entry=100.0, stop_loss=95.0, target=115.0,
        risk_reward=3.0, headline="Strong setup", narrative="", sector="it",
        vix_regime="normal", sector_rank=1,
        horizon="Swing (3-10 trading days)", valid_until="2026-08-01",
    )

    monkeypatch.setattr("analysis.score.score_stock", lambda tk, **_kw: fake_score)

    result = cache_mod._tomorrow_watchlist(n=15)
    all_items = (result["breakout_candidates"] + result["breakdown_watch"]
                + result["reversal_watch"])
    assert len(all_items) >= 1
    assert all_items[0]["horizon"] == "Swing (3-10 trading days)"
    assert all_items[0]["valid_until"] == "2026-08-01"


# ─────────────── session-aware freshness (snapshot vs latest closed session) ───
def _ist(y, m, d, hh, mm):
    import datetime as _d
    return _d.datetime(y, m, d, hh, mm, tzinfo=_d.timezone(_d.timedelta(hours=5, minutes=30)))


def test_holiday_weekend_snapshot_stays_current():
    """Scan on the Gandhi Jayanti holiday (Fri 2 Oct 2026) priced Thu 1 Oct,
    which is still the latest closed session at 02:00 IST Mon 5 Oct."""
    import dashboard.shared.cache as c
    assert c._tw_snapshot_current(_ist(2026, 10, 2, 16, 12).isoformat(), now=_ist(2026, 10, 5, 2, 0))


def test_overnight_post_close_snapshot_stays_current():
    import dashboard.shared.cache as c
    gen = _ist(2026, 9, 30, 16, 10).isoformat()                      # Wed post-close scan
    assert c._tw_snapshot_current(gen, now=_ist(2026, 10, 1, 1, 30))  # 9h later, next night
    assert c._tw_snapshot_current(gen, now=_ist(2026, 10, 1, 14, 0))  # next day, before close


def test_snapshot_goes_stale_after_next_close_plus_grace():
    import dashboard.shared.cache as c
    gen = _ist(2026, 9, 29, 16, 10).isoformat()                        # Tue post-close
    assert c._tw_snapshot_current(gen, now=_ist(2026, 9, 30, 17, 0))     # Wed close +1.5h: grace
    assert not c._tw_snapshot_current(gen, now=_ist(2026, 9, 30, 19, 0))  # Wed close +3.5h


def test_intraday_scan_does_not_count_as_that_sessions_close():
    import dashboard.shared.cache as c
    gen = _ist(2026, 9, 30, 11, 0).isoformat()                          # mid-session scan
    assert not c._tw_snapshot_current(gen, now=_ist(2026, 10, 1, 2, 0))  # 15h later


def test_bad_timestamp_is_not_current():
    import dashboard.shared.cache as c
    assert not c._tw_snapshot_current("not a time")
    assert not c._tw_snapshot_current(None)
