"""Ranked-list exclusions from edge flags: locked circuit, new listing (<60
sessions), BZ series stay off Top Picks / Tomorrow's Watchlist / screener
ranking, while their scores remain available elsewhere. Offline."""
import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from analysis.edge_flags import list_exclusion

_TODAY = dt.date(2026, 10, 5)


def _frame(n=300, end="2026-10-05", last=None):
    idx = pd.bdate_range(end=end, periods=n)
    c = np.linspace(100, 120, n)
    df = pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                       "Volume": 1e5}, index=idx)
    for k, v in (last or {}).items():
        df.iloc[-1, df.columns.get_loc(k)] = v
    return df


def _locked(mult):
    pc = float(_frame()["Close"].iloc[-2])
    lim = round(pc * mult, 2)
    return _frame(last={"Open": lim, "High": lim, "Low": lim, "Close": lim})


def test_normal_stock_is_listed():
    assert list_exclusion(_frame(), {"series": "EQ", "band": 20}, today=_TODAY) is None


def test_locked_circuits_are_excluded():
    assert list_exclusion(_locked(1.05), {"band": 5}, today=_TODAY) == "locked at upper circuit"
    assert list_exclusion(_locked(0.95), {"band": 5}, today=_TODAY) == "locked at lower circuit"


def test_touched_but_trading_circuit_stays_listed():
    pc = float(_frame()["Close"].iloc[-2])
    lim = round(pc * 0.80, 2)
    df = _frame(last={"Open": pc, "High": pc * 1.01, "Low": lim, "Close": lim})
    assert list_exclusion(df, {"band": 20}, today=_TODAY) is None


def test_new_listing_and_bz_are_excluded_short_history_is_not():
    assert list_exclusion(_frame(n=30), {}, today=_TODAY).startswith("new listing")
    assert list_exclusion(_frame(), {"series": "BZ"}, today=_TODAY) == "BZ series"
    # 60-250 sessions is an amber caveat, not an exclusion
    assert list_exclusion(_frame(), {"listed_on": dt.date(2026, 3, 2)}, today=_TODAY) is None
    # BE (trade-to-trade) and SME are amber: shown, not excluded
    assert list_exclusion(_frame(), {"series": "BE"}, today=_TODAY) is None


def test_never_raises_on_junk():
    assert list_exclusion(None, None) is None
    # no OHLC columns: circuit check skips; one bar still reads as a new listing
    assert list_exclusion(pd.DataFrame({"x": [1]}), {"band": "?"}).startswith("new listing")


def test_peek_cached_never_fetches(monkeypatch):
    import data.fetcher as F
    assert F.peek_cached("NOT_CACHED_ANYWHERE.NS") is None
    df = _frame(n=5)
    F._cache_put(("PEEK.NS", "2y", "1d"), df, __import__("time").time())
    got = F.peek_cached("PEEK.NS")
    assert got is not None and len(got) == 5 and got is not df


def test_excluded_note():
    from dashboard.shared.edge_flags_ui import excluded_note
    assert excluded_note({}) is None and excluded_note(None) is None
    txt = excluded_note({"ABC.NS": "BZ series", "NEW.NS": "new listing (under 60 sessions)"})
    assert "ABC (BZ series)" in txt and "NEW (new listing" in txt
    assert "and 2 more" in excluded_note({f"T{i}.NS": "BZ series" for i in range(10)})
    assert not any(w in txt.lower() for w in ("buy", "sell", "avoid"))


# ── scans ────────────────────────────────────────────────────────────────────
_EXCL = {"LOCKED.NS": "locked at upper circuit", "NEWCO.NS": "new listing (under 60 sessions)"}


def _fake_score(tk, **_kw):
    return SimpleNamespace(
        ticker=tk, price=100.0, score=60, grade="B", action="BUY", headline="h", entry=100.0,
        stop_loss=95.0, target=110.0, risk_reward=2.0, technical_score=30,
        momentum_score=10, volume_score=6, sentiment_score=5, horizon="", valid_until="",
        signals=[], key_levels={},
    )


@pytest.fixture
def scan_env(monkeypatch):
    import analysis.score as score_mod
    import data.universe as uni_mod
    from dashboard.shared import cache as cache_mod
    monkeypatch.setattr(uni_mod, "get_universe", lambda name: ["OK.NS", "LOCKED.NS", "NEWCO.NS"])
    monkeypatch.setattr(score_mod, "score_stock", _fake_score)
    monkeypatch.setattr(cache_mod, "_list_exclusion", lambda tk, period="2y": _EXCL.get(tk))
    monkeypatch.setattr(cache_mod, "_sector_ranks_tuple", lambda: (), raising=False)
    return cache_mod


def test_tomorrow_watchlist_drops_excluded(scan_env):
    cache_mod = scan_env
    cache_mod._tomorrow_watchlist.clear()
    try:
        out = cache_mod._tomorrow_watchlist(n=15)
    finally:
        cache_mod._tomorrow_watchlist.clear()
    assert [p["ticker"] for p in out["breakout_candidates"]] == ["OK.NS"]
    assert out["excluded"] == _EXCL
    assert out["breakout_funnel"]["eligible"] == 1


def test_top_picks_drop_excluded_but_keep_their_scores(scan_env):
    cache_mod = scan_env
    fn = getattr(cache_mod._home_top_picks, "__wrapped__", cache_mod._home_top_picks)
    if hasattr(cache_mod._home_top_picks, "clear"):
        cache_mod._home_top_picks.clear()
    out = fn()
    assert [b["ticker"] for b in out["buys"]] == ["OK.NS"]
    assert out["meta"]["excluded"] == _EXCL
    # the full scored map still serves My Watchlist / Analyze with these names
    assert set(out["all_scored"]) == {"OK.NS", "LOCKED.NS", "NEWCO.NS"}
