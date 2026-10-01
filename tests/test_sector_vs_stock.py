"""research/sector_vs_stock.py — pure analysis on synthetic data with a known answer."""
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from research import sector_vs_stock as svs  # noqa: E402

SECTORS = [f"S{i}" for i in range(8)]
# Realistic sector sizes: with only ~6 names the sector median is largely those
# names' own moves, so a pure stock signal leaks into "sector timing" (a real
# limit, noted in the report for small sectors).
PER_SECTOR = 30


def _world(kind: str, weeks: int = 40, seed: int = 0):
    """Synthetic 1-period world. kind='sector': score knows next-week sector
    moves only; kind='stock': score knows each stock's move within its sector."""
    rng = np.random.default_rng(seed)
    tickers = [f"{s}_{j}" for s in SECTORS for j in range(PER_SECTOR)]
    sector_of = {t: t.split("_")[0] for t in tickers}
    days = pd.bdate_range("2025-01-06", periods=weeks + 2)
    rows, closes = [], {t: [100.0] for t in tickers}
    for w in range(weeks + 1):
        sec_move = {s: rng.normal(0, 2) for s in SECTORS}
        idio = {t: rng.normal(0, 2) for t in tickers}
        for t in tickers:
            r = sec_move[sector_of[t]] + idio[t]
            closes[t].append(closes[t][-1] * (1 + r / 100))
            if w < weeks:
                signal = sec_move[sector_of[t]] if kind == "sector" else idio[t]
                rows.append({"ticker": t, "date": days[w].strftime("%Y-%m-%d"),
                             "sector": sector_of[t], "score": 50 + signal + rng.normal(0, 0.3)})
    close_series = {t: pd.Series(v, index=days[: len(v)]) for t, v in closes.items()}
    obs = pd.DataFrame(rows)
    panel = svs.forward_return_panel(close_series, 1)
    obs["fwd_1d"] = [panel.loc[pd.Timestamp(d), t] for d, t in zip(obs["date"], obs["ticker"])]
    sect = svs.sector_medians(panel, sector_of)
    return svs.decompose(obs, sect, 1)


@pytest.fixture(autouse=True)
def _small_thresholds(monkeypatch):
    monkeypatch.setattr(svs, "MIN_STOCKS_PER_WEEK", 20)


def test_parts_add_up_to_the_return():
    o = _world("stock")
    total = o["market_1d"] + o["sector_part_1d"] + o["stock_part_1d"]
    assert np.allclose(total, o["fwd_1d"], equal_nan=True)


def test_sector_signal_shows_up_as_sector_timing_not_stock_selection():
    o = _world("sector")
    st, sk = svs.sector_timing_ic(o, 1), svs.stock_selection_ic(o, 1)
    assert st["ic_mean"] > 0.8 and st["ic_t"] > 5
    assert abs(sk["ic_mean"]) < 0.1
    q = svs.quintile_split(o, 1)
    assert q["spread_sector"] > 2 * abs(q["spread_stock"])


def test_stock_signal_shows_up_as_stock_selection_not_sector_timing():
    o = _world("stock")
    st, sk = svs.sector_timing_ic(o, 1), svs.stock_selection_ic(o, 1)
    assert sk["ic_mean"] > 0.6 and sk["ic_t"] > 5
    assert abs(st["ic_mean"]) < 0.25                      # sector mean of idio noise ≈ noise
    q = svs.quintile_split(o, 1)
    assert q["spread_stock"] > 2 * abs(q["spread_sector"])


def test_sector_median_needs_min_names():
    panel = pd.DataFrame({"A_1": [1.0], "A_2": [2.0], "B_1": [3.0]},
                         index=pd.to_datetime(["2025-01-06"]))
    s = svs.sector_medians(panel, {"A_1": "A", "A_2": "A", "B_1": "B"}, min_names=2)
    assert s.loc["2025-01-06", "A"] == 1.5
    assert np.isnan(s.loc["2025-01-06", "B"])
    assert s.loc["2025-01-06", "MARKET"] == 2.0


def test_hold_out_drops_windows_reaching_the_live_period():
    days = pd.bdate_range("2026-08-17", "2026-09-10")
    obs = pd.DataFrame({"date": ["2026-08-17", "2026-08-24", "2026-08-27"]})
    kept = svs.hold_out_live_period(obs, days, h=5, cutoff=dt.date(2026, 8, 29))
    # 17 Aug + 5 → 24 Aug (kept); 24 Aug + 5 → 31 Aug (dropped); 27 Aug + 5 → 3 Sep (dropped)
    assert list(kept["date"]) == ["2026-08-17"]


def test_summarise_and_report_render():
    o = _world("sector")
    o["live_regime"] = np.where(pd.to_datetime(o["date"]).dt.day < 15, "a", "b")
    s = pd.concat([svs.summarise(o, horizons=(1,)),
                   svs.summarise(o, horizons=(1,), slice_col="live_regime")], ignore_index=True)
    assert set(s["slice"]) == {"all", "a", "b"}
    md = svs.render_report(s, {"generated": "x", "period": "2y", "tickers": "48", "weeks": "40"})
    assert "Sector-timing IC" in md and "not investment advice" in md
