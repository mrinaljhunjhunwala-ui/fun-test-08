"""Breadth snapshot from scan price frames, its KV round-trip, the read order
(persisted -> bhavcopy disk -> None) and the contiguous PREV_CLOSE fill."""
from __future__ import annotations

import datetime as _dt
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.market_breadth import breadth_from_price_frames, frames_to_long  # noqa: E402
import data.market_breadth_loader as mbl  # noqa: E402


def _frames(n_up: int, n_down: int, days: int = 260) -> dict:
    idx = pd.bdate_range("2025-09-01", periods=days)
    out = {}
    for k in range(n_up):
        out[f"U{k}.NS"] = pd.DataFrame({"Open": 1, "High": 1, "Low": 1,
                                        "Close": 100 * 1.002 ** np.arange(days), "Volume": 1e5}, index=idx)
    for k in range(n_down):
        out[f"D{k}.NS"] = pd.DataFrame({"Close": 100 * 0.998 ** np.arange(days), "Volume": 1e5}, index=idx)
    return out


def test_breadth_from_price_frames():
    snap = breadth_from_price_frames(_frames(30, 10))
    assert snap["advances"] == 30 and snap["declines"] == 10
    assert snap["universe_size"] == 40
    assert snap["pct_above_20dma"] == 75.0 and snap["pct_above_200dma"] == 75.0
    assert snap["new_52w_highs"] == 30 and snap["new_52w_lows"] == 10
    assert snap["date"] == pd.bdate_range("2025-09-01", periods=260)[-1].date().isoformat()


def test_frames_cut_to_common_last_date_and_too_few_is_none():
    f = _frames(25, 0)
    extra = pd.bdate_range("2025-09-01", periods=261)
    f["LATE.NS"] = pd.DataFrame({"Close": np.linspace(100, 90, 261)}, index=extra)
    long = frames_to_long(f)
    assert long["DATE"].max() == pd.Timestamp(extra[-2])
    assert breadth_from_price_frames(_frames(5, 5)) is None
    assert breadth_from_price_frames({}) is None


def test_snapshot_from_scan_uses_peek_only():
    fr = _frames(20, 20)
    snap = mbl.snapshot_from_scan(list(fr) + ["MISSING.NS"], "niftytotalmarket",
                                  peek=lambda tk, period: fr.get(tk))
    assert snap["source"] == "scan" and snap["universe"] == "niftytotalmarket"
    assert snap["universe_size"] == 40


def _kv():
    store = {}
    return store, (lambda k, v, user_id: store.__setitem__((k, user_id), v) or True), \
        (lambda k, user_id: store.get((k, user_id)))


def test_persist_read_round_trip_and_freshness():
    _, kv_set, kv_get = _kv()
    fr = _frames(30, 10)
    snap = mbl.snapshot_from_scan(list(fr), "niftytotalmarket", peek=lambda tk, period: fr.get(tk))
    assert mbl.persist_snapshot(snap, kv_set=kv_set)
    asof = _dt.date.fromisoformat(snap["date"])
    got = mbl.persisted_snapshot(today=asof + _dt.timedelta(days=4), kv_get=kv_get)
    assert got["advances"] == 30 and got["universe_size"] == 40 and got["generated_at"]
    assert mbl.persisted_snapshot(today=asof + _dt.timedelta(days=5), kv_get=kv_get) is None
    assert mbl.persist_snapshot(None, kv_set=kv_set) is False


def test_latest_snapshot_order(monkeypatch):
    _, kv_set, kv_get = _kv()
    monkeypatch.setattr(mbl, "breadth_snapshot", lambda **kw: {"source": "bhavcopy", "kw": kw})
    # nothing persisted -> disk computation
    assert mbl.latest_snapshot(max_downloads=0, kv_get=kv_get)["source"] == "bhavcopy"
    mbl.persist_snapshot({"date": "2026-10-08", "advances": 1}, kv_set=kv_set)
    fresh = mbl.latest_snapshot(today=_dt.date(2026, 10, 9), kv_get=kv_get)
    assert fresh["source"] == "scan"
    stale = mbl.latest_snapshot(today=_dt.date(2026, 10, 20), kv_get=kv_get)
    assert stale["source"] == "bhavcopy"
    # broken KV never raises
    assert mbl.persisted_snapshot(kv_get=lambda *a, **k: 1 / 0) is None
    monkeypatch.setattr(mbl, "breadth_snapshot", lambda **kw: None)
    assert mbl.latest_snapshot(kv_get=lambda *a, **k: None) is None


def test_breadth_shadow_prefers_persisted(monkeypatch):
    import analysis.verdict_ledger as vl
    monkeypatch.setattr(vl, "_breadth_shadow_cache", {"t": 0.0, "val": (None, None)})
    monkeypatch.setattr(mbl, "persisted_snapshot",
                        lambda **kw: {"breadth_label": "bear", "pct_above_50dma": 31.234})
    monkeypatch.setattr(mbl, "breadth_snapshot", lambda **kw: 1 / 0)
    assert vl._breadth_shadow() == ("bear", 31.23)


def test_fill_prev_close_nulls_across_gaps():
    # 2026-10-05 (Mon), 10-06 (Tue) cached; 10-07 missing; 10-08 cached.
    df = pd.DataFrame({"SYMBOL": ["A"] * 3, "DATE": ["2026-10-05", "2026-10-06", "2026-10-08"],
                       "Close": [100.0, 101.0, 105.0]})
    out = mbl._fill_prev_close(df).set_index("DATE")["PREV_CLOSE"]
    assert out["2026-10-06"] == 100.0
    assert pd.isna(out["2026-10-08"])        # previous cached session is not 10-07
    assert pd.isna(out["2026-10-05"])


def test_snapshot_from_scan_skips_partial_universe():
    # Fetch cache expired for most tickers (slow scan): must not persist partial breadth.
    import data.market_breadth_loader as mbl
    tickers = [f"T{i}.NS" for i in range(100)]
    frames = {tk: None for tk in tickers}
    assert mbl.snapshot_from_scan(tickers, "niftytotalmarket", peek=lambda tk, period=None: frames[tk]) is None
