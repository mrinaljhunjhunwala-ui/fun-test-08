"""Volume pillar fixes (audit 2026-10-03, findings SC3 / SC4 / E1 / E4).

* up_day was close >= open: a gap-up that fades but still closes +3% on 3x
  volume was scored as a DOWN day (volume points x0.4).
* Volume_Ratio's 20-day mean included today's bar, damping the spike it
  measures (a 3x day read ~2.5x).
* During market hours today's daily bar holds a partial session's volume, so
  every stock read as "low volume" in the morning and drifted up all day.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from analysis import score as S
from utils.indicators import add_all_indicators


def _frame(n=120, last=None, seed=7, end="2026-10-01"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=end, periods=n)
    c = 100 * np.cumprod(1 + rng.normal(0.0005, 0.01, n))
    o = c * (1 + rng.normal(0, 0.003, n))
    df = pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.004,
                       "Low": np.minimum(o, c) * 0.996, "Close": c,
                       "Volume": np.full(n, 100_000.0)}, index=idx)
    if last is not None:
        df.iloc[-1] = [last[k] for k in ("Open", "High", "Low", "Close", "Volume")]
    return df


def test_volume_ratio_excludes_todays_bar():
    df = _frame()
    df.iloc[-1, df.columns.get_loc("Volume")] = 300_000.0
    out = add_all_indicators(df)
    assert out["Volume_Ratio"].iloc[-1] == pytest.approx(3.0)


def test_gap_up_that_fades_but_closes_higher_is_an_up_day():
    df = _frame()
    prev = float(df["Close"].iloc[-2])
    df = _frame(last={"Open": prev * 1.05, "High": prev * 1.06, "Low": prev * 1.025,
                      "Close": prev * 1.03, "Volume": 300_000.0})
    pts, detail = S._score_volume(add_all_indicators(df))
    assert detail["up_day"] is True
    assert detail["vol_ratio"] == 10.0          # 3x on an up day = full points


def test_red_candle_on_a_down_close_is_still_a_down_day():
    df = _frame()
    prev = float(df["Close"].iloc[-2])
    df = _frame(last={"Open": prev * 0.99, "High": prev * 0.995, "Low": prev * 0.97,
                      "Close": prev * 0.975, "Volume": 300_000.0})
    _, detail = S._score_volume(add_all_indicators(df))
    assert detail["up_day"] is False
    assert detail["vol_ratio"] == 4.0           # 10 x 0.4


def test_in_progress_session_scores_the_last_completed_bar():
    # 11:00 IST on a trading day: today's bar holds ~a third of a day's volume.
    df = _frame(end="2026-10-05")
    df.iloc[-1, df.columns.get_loc("Volume")] = 30_000.0
    df.iloc[-2, df.columns.get_loc("Volume")] = 200_000.0
    ind = add_all_indicators(df)
    now = dt.datetime(2026, 10, 5, 11, 0)
    _, detail = S._score_volume(ind, now_ist=now)
    assert detail["volume_session"] == "previous (today in progress)"
    assert detail["vol_ratio_raw"] == pytest.approx(2.0)


def test_completed_session_scores_today():
    df = _frame(end="2026-10-05")
    ind = add_all_indicators(df)
    _, detail = S._score_volume(ind, now_ist=dt.datetime(2026, 10, 5, 16, 0))
    assert detail["volume_session"] == "latest"


def test_bar_in_progress_detection():
    idx = pd.DatetimeIndex(["2026-10-01", "2026-10-05"])
    df = pd.DataFrame({"Close": [1.0, 1.0]}, index=idx)
    assert S._last_bar_in_progress(df, dt.datetime(2026, 10, 5, 10, 0)) is True
    assert S._last_bar_in_progress(df, dt.datetime(2026, 10, 5, 15, 31)) is False
    assert S._last_bar_in_progress(df, dt.datetime(2026, 10, 6, 10, 0)) is False   # stale bar
    intraday = pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex(["2026-10-05 10:05"]))
    assert S._last_bar_in_progress(intraday, dt.datetime(2026, 10, 5, 10, 7)) is False
