"""Volume pillar fixes from the 2026-10-03 audit.

#16 — "up day" is close vs the previous close, not close vs open.
#17 — Volume_Ratio's 20-day baseline excludes the bar being measured.
"""
import numpy as np
import pandas as pd
import pytest

from analysis.score import _score_volume
from utils.indicators import add_volume_indicators


def _frame(n=40, vol=1_000_000.0):
    idx = pd.bdate_range("2026-01-01", periods=n)
    close = np.linspace(100.0, 110.0, n)
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": np.full(n, vol),
    }, index=idx)


def test_volume_ratio_excludes_today_from_its_own_baseline():
    df = _frame()
    df.loc[df.index[-1], "Volume"] = 3_000_000.0       # 3x a flat 1M history
    out = add_volume_indicators(df)
    assert out["Volume_SMA_20"].iloc[-1] == pytest.approx(1_000_000.0)
    assert out["Volume_Ratio"].iloc[-1] == pytest.approx(3.0)


def test_volume_ratio_still_nan_not_inf_when_prior_window_is_zero():
    df = _frame(n=60)
    df.loc[df.index[:40], "Volume"] = 0.0
    out = add_volume_indicators(df)
    assert not np.isinf(out["Volume_Ratio"].to_numpy()).any()
    assert np.isnan(out["Volume_Ratio"].iloc[40])     # baseline = 20 zero bars


def _scored_last_bar(open_, close, prev_close, vol_ratio=3.0):
    df = add_volume_indicators(_frame())
    df.loc[df.index[-2], "Close"] = prev_close
    last = df.index[-1]
    df.loc[last, ["Open", "Close", "High", "Low"]] = [open_, close, max(open_, close), min(open_, close)]
    df.loc[last, "Volume_Ratio"] = vol_ratio
    return _score_volume(df)[1]["vol_ratio"]


def test_gap_up_that_fades_keeps_full_volume_points():
    # Prev close 100, opens 106, closes 103: +3% on the day on 3x volume.
    assert _scored_last_bar(open_=106.0, close=103.0, prev_close=100.0) == 10.0


def test_gap_down_that_recovers_partly_is_still_a_down_day():
    # Prev close 100, opens 94, closes 97: -3% on the day despite close > open.
    assert _scored_last_bar(open_=94.0, close=97.0, prev_close=100.0) == pytest.approx(4.0)


def test_single_bar_frame_falls_back_to_close_vs_open():
    df = add_volume_indicators(_frame(n=1))
    df["Volume_Ratio"] = 3.0
    df["Open"], df["Close"] = 100.0, 99.0
    assert _score_volume(df)[1]["vol_ratio"] == pytest.approx(4.0)
