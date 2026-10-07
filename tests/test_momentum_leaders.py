"""Momentum leaders: the pure ranking helper, the cache wiring that reuses a
scan's cached frames, and the universe additions (CUPID, DHANBANK)."""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.momentum_leaders import momentum_metrics, rank_momentum_leaders  # noqa: E402


def _frame(drift: float, n: int = 260, price: float = 100.0, volume: float = 1e6,
           vol_tail: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = price * np.exp(np.cumsum(drift + rng.normal(0, 0.005, n)))
    vol = np.full(n, float(volume))
    vol[-5:] *= vol_tail
    idx = pd.bdate_range("2025-09-01", periods=n)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": vol}, index=idx)


def _no_excl(_tk, _df):
    return None


def test_score_bounded_with_components():
    m = momentum_metrics(_frame(0.004))
    assert 0 <= m["momentum_score"] <= 100
    assert set(m["components"]) == {"ret20", "ret60", "near_high", "ma_stack", "adx", "volume"}
    assert m["above_50dma"] and m["above_200dma"]
    assert momentum_metrics(_frame(0.004, n=50)) is None


def test_ranking_order_strong_first():
    frames = {"UP.NS": _frame(0.006, vol_tail=3), "FLAT.NS": _frame(0.0),
              "DOWN.NS": _frame(-0.004)}
    out = rank_momentum_leaders(frames, exclusion=_no_excl)
    assert [r["ticker"] for r in out["leaders"]] == ["UP.NS", "FLAT.NS", "DOWN.NS"]
    assert out["leaders"][0]["rank"] == 1
    assert out["leaders"][0]["momentum_score"] > out["leaders"][-1]["momentum_score"]


def test_exclusions_and_floors():
    frames = {
        "OK.NS": _frame(0.004),
        "LOCKED.NS": _frame(0.006),
        "ILLIQ.NS": _frame(0.006, volume=1000),          # ~Rs 1-3 lakh/day
        "PENNY.NS": _frame(0.002, price=5, volume=1e8),
    }
    out = rank_momentum_leaders(
        frames, exclusion=lambda tk, _df: "locked at upper circuit" if tk == "LOCKED.NS" else None,
        extra={"OK.NS": {"sector": "IT", "composite_action": "HOLD"}})
    assert [r["ticker"] for r in out["leaders"]] == ["OK.NS"]
    assert out["leaders"][0]["sector"] == "IT"
    assert out["n_filtered"] == {"locked at upper circuit": 1, "liquidity floor": 1,
                                 "price floor": 1}


def test_default_exclusion_drops_degraded_source():
    df = _frame(0.006)
    df.attrs["source"] = "NSE-bhavcopy"
    out = rank_momentum_leaders({"BHAV.NS": df, "OK.NS": _frame(0.004)})
    assert [r["ticker"] for r in out["leaders"]] == ["OK.NS"]


def test_cache_reuses_scan_frames_without_fetching(monkeypatch):
    from dashboard.shared import cache as cache_mod
    import data.fetcher as fetcher

    frames = {"A.NS": _frame(0.006), "B.NS": _frame(0.001)}
    seen = []

    def _peek(tk, period="2y", interval="1d"):
        seen.append((tk, period))
        return frames.get(tk)

    monkeypatch.setattr(fetcher, "peek_cached", _peek)
    monkeypatch.setattr(fetcher, "fetch_single",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    monkeypatch.setattr(cache_mod, "_list_exclusion", lambda tk, period="2y": None)
    out = cache_mod._momentum_leaders_from_scan(
        ["A.NS", "B.NS", "MISSING.NS"], {"A.NS": {"score": 44, "action": "HOLD"}})
    assert [r["ticker"] for r in out["leaders"]] == ["A.NS", "B.NS"]
    assert out["leaders"][0]["composite_action"] == "HOLD"
    assert out["n_filtered"] == {"no data": 1}
    assert {p for _, p in seen} == {"2y"}


def test_get_momentum_leaders_reads_snapshots(monkeypatch):
    from dashboard.shared import cache as cache_mod
    lst = {"leaders": [{"ticker": "A.NS", "rank": 1}], "n_ranked": 1}
    store = {cache_mod._TW_KV_KEY: {"data": {"momentum_leaders": lst}, "generated_at": "g"}}
    monkeypatch.setattr(cache_mod._store, "kv_get", lambda key, user_id=None: store.get(key))
    got = cache_mod.get_momentum_leaders()
    assert got["leaders"][0]["ticker"] == "A.NS" and got["generated_at"] == "g"
    store[cache_mod._MOMENTUM_KV_KEY] = {"data": {"leaders": [{"ticker": "Z.NS"}]},
                                         "generated_at": "h"}
    assert cache_mod.get_momentum_leaders()["leaders"][0]["ticker"] == "Z.NS"
    monkeypatch.setattr(cache_mod._store, "kv_get", lambda key, user_id=None: None)
    assert cache_mod.get_momentum_leaders() is None


def test_tw_scan_carries_momentum_leaders(monkeypatch):
    from types import SimpleNamespace
    import analysis.score as score_mod
    import data.fetcher as fetcher
    import data.universe as uni_mod
    from dashboard.shared import cache as cache_mod

    seen_levels = []
    monkeypatch.setattr(uni_mod, "get_universe",
                        lambda name: seen_levels.append(name) or ["A.NS"])
    monkeypatch.setattr(score_mod, "score_stock", lambda tk, **_k: SimpleNamespace(
        price=100.0, score=44, grade="C", action="HOLD", headline="h", entry=100.0,
        stop_loss=95.0, target=110.0, risk_reward=2.0, technical_score=20,
        momentum_score=10, volume_score=5, horizon="", valid_until=""))
    monkeypatch.setattr(fetcher, "peek_cached", lambda tk, period="2y", interval="1d": _frame(0.006))
    monkeypatch.setattr(cache_mod, "_list_exclusion", lambda tk, period="2y": None)
    cache_mod._tomorrow_watchlist.clear()
    try:
        out = cache_mod._tomorrow_watchlist(n=15)
    finally:
        cache_mod._tomorrow_watchlist.clear()
    assert seen_levels == ["niftytotalmarket"]
    assert out["momentum_leaders"]["leaders"][0]["ticker"] == "A.NS"
    assert out["momentum_leaders"]["leaders"][0]["composite_score"] == 44


def test_universe_contains_cupid_and_dhanbank():
    from data.universe import get_sector, get_universe
    u = get_universe("niftytotalmarket")
    assert "CUPID.NS" in u and "DHANBANK.NS" in u
    assert len(u) == len(set(u))
    assert get_sector("DHANBANK.NS") == "Banking"


def test_momentum_ledger_counts_only_new_rows(monkeypatch):
    import importlib
    import analysis.verdict_ledger as VL
    ids = iter([7, None])
    monkeypatch.setattr(VL, "log_verdict", lambda **k: next(ids))
    wtp = importlib.import_module("scripts.warm_top_picks")
    rows = [{"ticker": "A.NS", "rank": 1, "momentum_score": 80, "composite_score": 44,
             "composite_action": "HOLD", "price": 10.0},
            {"ticker": "B.NS", "rank": 2, "momentum_score": 70}]
    assert wtp.log_momentum_leaders(rows) == 1


def test_momentum_failure_never_blocks_shadow_log(monkeypatch):
    import importlib
    import dashboard.shared.cache as cache_mod
    import dashboard.shared.market_hours as mh
    import trade_store
    wtp = importlib.import_module("scripts.warm_top_picks")
    monkeypatch.setattr(mh, "is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(cache_mod, "get_vix_info", lambda: {"regime": "normal"})
    monkeypatch.setattr(cache_mod, "_sector_ranks_tuple", lambda: ())
    monkeypatch.setattr(cache_mod, "_home_top_picks", lambda **k: {
        "buys": [{"ticker": "AAA.NS"}], "sells": [], "meta": {}, "all_scored": {},
        "momentum_leaders": {"leaders": [{"ticker": "X.NS"}]}})
    monkeypatch.setattr(trade_store, "kv_set", lambda *a, **k: True)

    def boom(_rows):
        raise RuntimeError("ledger down")
    shadow = []
    monkeypatch.setattr(wtp, "log_momentum_leaders", boom)
    monkeypatch.setattr(wtp, "log_shadow_picks", lambda buys: shadow.append(buys) or 1)
    assert wtp.main() == 0
    assert shadow == [[{"ticker": "AAA.NS"}]]
