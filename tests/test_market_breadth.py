"""Unit tests for analysis/market_breadth.py (pure, offline)."""
import ast
import pathlib

import numpy as np
import pandas as pd

from analysis.market_breadth import breadth_regime, build_panels, compute_breadth, smoothed


def _sessions(closes: dict, prev: dict = None, vols: dict = None, series="EQ") -> dict:
    """closes: {symbol: [c0, c1, ...]} -> {date: frame}."""
    dates = pd.bdate_range("2024-01-01", periods=len(next(iter(closes.values()))))
    out = {}
    for i, d in enumerate(dates):
        rows = []
        for s, cs in closes.items():
            if cs[i] is None:
                continue
            p = (prev or {}).get(s, [None] * len(cs))[i]
            if p is None:
                p = cs[i - 1] if i > 0 and cs[i - 1] is not None else cs[i]
            rows.append({"SYMBOL": s, "SERIES": series, "Close": cs[i], "PREV_CLOSE": p,
                         "Volume": (vols or {}).get(s, [100] * len(cs))[i]})
        out[d.date()] = pd.DataFrame(rows)
    return out


def test_advances_declines_unchanged_and_ad_line():
    s = _sessions({"A": [10, 11, 12], "B": [10, 9, 9], "C": [10, 10, 11]})
    b = compute_breadth(s)
    last = b.iloc[-1]
    assert (last.advances, last.declines, last.unchanged) == (2, 0, 1)
    mid = b.iloc[1]
    assert (mid.advances, mid.declines, mid.unchanged) == (1, 1, 1)
    assert mid.ad_ratio == 1.0
    assert b["ad_line"].iloc[-1] == 2


def test_prev_close_neutralises_a_split():
    # 1:1 bonus on day 3: raw close halves, but NSE's adjusted PREV_CLOSE does too.
    closes = {"A": [100.0, 102, 51.5, 52]}
    prev = {"A": [None, None, 51.0, None]}
    p = build_panels(_sessions(closes, prev))
    r = p["ret"]["A"]
    assert abs(r.iloc[2] - (51.5 / 51 - 1)) < 1e-12
    assert p["adj"]["A"].iloc[-1] > p["adj"]["A"].iloc[0]           # no fake crash
    assert compute_breadth(_sessions(closes, prev)).iloc[2].advances == 1


def test_pct_above_dma_needs_history():
    n = 60
    up = list(np.linspace(10, 20, n))
    down = list(np.linspace(20, 10, n))
    late = [None] * 40 + list(np.linspace(10, 30, 20))      # only 20 sessions of history
    b = compute_breadth(_sessions({"UP": up, "DN": down, "LATE": late}))
    assert np.isnan(b["pct_above_50dma"].iloc[30])
    assert b["pct_above_50dma"].iloc[-1] == 50.0            # UP above, DN below, LATE excluded
    assert b["pct_above_20dma"].iloc[-1] == 100.0 * 2 / 3
    assert np.isnan(b["pct_above_200dma"].iloc[-1])


def test_new_highs_lows_and_up_volume():
    n = 260
    up = list(np.linspace(10, 30, n))
    dn = list(np.linspace(30, 10, n))
    b = compute_breadth(_sessions({"UP": up, "DN": dn}, vols={"UP": [300] * n, "DN": [100] * n}))
    assert b["new_52w_highs"].iloc[-1] == 1 and b["new_52w_lows"].iloc[-1] == 1
    assert np.isnan(b["new_52w_highs"].iloc[100])
    assert b["up_volume"].iloc[-1] == 300 and b["down_volume"].iloc[-1] == 100
    assert b["up_volume_ratio"].iloc[-1] == 0.75


def test_series_and_universe_filters():
    s = _sessions({"A": [10, 11], "B": [10, 9]})
    for d, f in s.items():
        s[d] = pd.concat([f, pd.DataFrame([{"SYMBOL": "Z", "SERIES": "BE", "Close": 5,
                                            "PREV_CLOSE": 1, "Volume": 1}])])
    b = compute_breadth(s)
    assert b.iloc[-1].n == 2                                  # BE row ignored
    assert compute_breadth(s, universe=["A.NS"]).iloc[-1].n == 1


def test_long_frame_input_and_no_prev_close():
    long = pd.DataFrame({"DATE": ["2024-01-01", "2024-01-02"] * 2, "SYMBOL": ["A", "A", "B", "B"],
                         "Close": [10, 11, 10, 9], "Volume": [1, 1, 1, 1]})
    b = compute_breadth(long)
    assert (b.iloc[-1].advances, b.iloc[-1].declines) == (1, 1)


def test_regime_labels():
    idx = pd.bdate_range("2024-01-01", periods=30)
    br = pd.DataFrame({"advances": 100, "declines": 300, "unchanged": 0,
                       "pct_above_50dma": 30.0}, index=idx)
    assert breadth_regime(br).iloc[-1] == "bear"
    br["pct_above_50dma"] = 70.0
    assert breadth_regime(br).iloc[-1] == "bull"
    br["pct_above_50dma"], br["advances"] = 30.0, 400        # narrow but A/D > 1 -> not bear
    assert breadth_regime(br).iloc[-1] == "neutral"
    assert smoothed(br)["ad_ratio"].iloc[-1] == 400 / 300


def test_module_is_streamlit_free():
    src = pathlib.Path(__file__).resolve().parents[1] / "analysis" / "market_breadth.py"
    mods = {n.names[0].name if isinstance(n, ast.Import) else n.module
            for n in ast.walk(ast.parse(src.read_text(encoding="utf-8")))
            if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert not any(m and m.startswith("streamlit") for m in mods)
