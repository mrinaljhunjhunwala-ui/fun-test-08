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


def test_in_progress_session_before_1015_scores_the_last_completed_bar():
    # 10:00 IST on a trading day: too early to project today's partial volume.
    df = _frame(end="2026-10-05")
    df.iloc[-1, df.columns.get_loc("Volume")] = 30_000.0
    df.iloc[-2, df.columns.get_loc("Volume")] = 200_000.0
    ind = add_all_indicators(df)
    now = dt.datetime(2026, 10, 5, 10, 0)
    _, detail = S._score_volume(ind, now_ist=now)
    assert detail["volume_session"] == "previous_session"
    assert detail["vol_ratio_raw"] == pytest.approx(2.0)
    assert detail["session_minutes"] == 45


def test_in_progress_session_from_1015_is_projected_to_a_full_day():
    # 11:15 IST (120 min): ~27.3% of a day's volume normally trades by now, so
    # 60k against a 100k baseline (0.6x so far) is on pace for ~2.2x.
    df = _frame(end="2026-10-05")
    df.iloc[-1, df.columns.get_loc("Volume")] = 60_000.0
    ind = add_all_indicators(df)
    r = S.volume_reading(ind, now_ist=dt.datetime(2026, 10, 5, 11, 15))
    assert r["basis"] == "projected" and r["session_minutes"] == 120
    assert r["ratio_raw"] == pytest.approx(0.6)
    assert r["ratio"] == pytest.approx(0.6 / 0.273, rel=1e-3)
    assert r["volume"] == pytest.approx(60_000 / 0.273, rel=1e-3)
    assert r["baseline"] == pytest.approx(100_000.0)
    _, detail = S._score_volume(ind, now_ist=dt.datetime(2026, 10, 5, 11, 15))
    assert detail["volume_session"] == "projected"
    assert detail["vol_ratio_bar"] == pytest.approx(0.6)
    assert detail["vol_ratio"] in (8.0, 8.0 * 0.4)   # 1.8-2.5x bucket, direction applied


def test_volume_reading_after_close_is_the_latest_bar():
    df = _frame(end="2026-10-05")
    df.iloc[-1, df.columns.get_loc("Volume")] = 250_000.0
    r = S.volume_reading(add_all_indicators(df), now_ist=dt.datetime(2026, 10, 5, 16, 0))
    assert r["basis"] == "latest" and r["session_minutes"] is None
    assert r["ratio"] == pytest.approx(2.5) and r["volume"] == pytest.approx(250_000.0)


def test_volume_reading_short_and_intraday_frames_read_latest_and_never_raise():
    now = dt.datetime(2026, 10, 5, 11, 15)
    short = add_all_indicators(_frame(n=15, end="2026-10-05"))
    assert S.volume_reading(short, now_ist=now)["basis"] == "latest"
    intraday = pd.DataFrame({"Open": [1.0, 1.0], "High": [1.0, 1.0], "Low": [1.0, 1.0],
                             "Close": [1.0, 1.1], "Volume": [10.0, 20.0]},
                            index=pd.DatetimeIndex(["2026-10-05 11:05", "2026-10-05 11:10"]))
    r = S.volume_reading(intraday, now_ist=now)
    assert r["basis"] == "latest" and r["up_day"] is True and r["ratio"] == 1.0
    assert S.volume_reading(pd.DataFrame(), now_ist=now)["ratio"] == 1.0


def test_narrative_names_the_session_it_scored():
    from tools.composite_golden import build_frame
    df = build_frame("volume_spike_up", with_rs=False)
    cs = S.score_dataframe(df, "TESTCO.NS", sector_rank=None)
    assert "Volume in the latest session was" in cs.narrative
    assert "Today's volume is on pace" not in cs.narrative


def _projected_frame():
    df = _frame(end="2026-10-05")
    df.iloc[-1, df.columns.get_loc("Volume")] = 60_000.0
    return add_all_indicators(df)


_AT_1115 = dt.datetime(2026, 10, 5, 11, 15)


def test_missing_ratio_is_not_projected_into_a_volume_spike():
    # Review 2026-10-06: the 1.0 fallback used to be divided by the curve
    # (1 / 0.273 = 3.7x at 11:15). Unknown must stay neutral.
    ind = _projected_frame()
    ind.iloc[-1, ind.columns.get_loc("Volume_Ratio")] = np.nan
    r = S.volume_reading(ind, now_ist=_AT_1115)
    assert r["basis"] == "projected"
    assert r["ratio"] == 1.0 and r["ratio_raw"] is None


def test_aware_clock_is_read_in_ist():
    ind = _projected_frame()
    utc = dt.datetime(2026, 10, 5, 5, 45, tzinfo=dt.timezone.utc)      # 11:15 IST
    assert S.volume_reading(ind, now_ist=utc)["basis"] == "projected"
    assert S.volume_reading(ind, now_ist=utc)["session_minutes"] == 120


def test_short_frame_obv_uses_completed_sessions_only():
    ind = add_all_indicators(_frame(n=15, end="2026-10-05"))
    reading, done = S._volume_reading(ind, now_ist=_AT_1115)
    assert reading["basis"] == "latest"
    assert len(done) == len(ind) - 1


def test_projected_bar_never_pairs_with_previous_session_delivery():
    ind = _projected_frame()
    ind.iloc[-1, ind.columns.get_loc("Close")] = float(ind["Close"].iloc[-2]) * 1.02   # up day
    deliv = {"today": 18.0, "mean": 45.0, "zscore": -1.6, "n": 60}
    _, detail = S._score_volume(ind, delivery_info=deliv, now_ist=_AT_1115)
    assert detail["volume_session"] == "projected" and detail["vol_ratio_raw"] > 1.5
    assert detail["delivery_divergence"] is False


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


def test_pre_open_bar_reads_previous_session_never_projected():
    # Yahoo can publish today's bar at 09:00-09:15 with zero volume; minutes
    # clamp to 0 and must never reach the projection (the curve is 0 there).
    df = _frame(end="2026-10-05")
    df.iloc[-1, df.columns.get_loc("Volume")] = 0.0
    r = S.volume_reading(add_all_indicators(df), now_ist=dt.datetime(2026, 10, 5, 9, 5))
    assert r["basis"] == "previous_session" and r["session_minutes"] == 0


def test_projected_volume_is_not_paired_with_todays_unfinished_candle():
    ind = add_all_indicators(_frame(end="2026-10-05"))
    ind.iloc[-1, ind.columns.get_loc("Volume_Ratio")] = 0.9          # ~3.3x on pace at 11:15
    ind.iloc[-1, ind.columns.get_loc("Close")] = float(ind["Close"].iloc[-2]) * 1.02
    _, vol_detail = S._score_volume(ind, now_ist=_AT_1115)
    narrative = S._build_narrative(
        "TESTCO.NS", ind, 60.0, "B", "WATCHLIST", {}, {}, vol_detail, ["Hammer"],
        100.0, 95.0, 110.0, 2.0, "Other", "normal", None, 15, False)[1]
    assert "Today's volume is on pace for" in narrative
    assert "Hammer candlestick" not in narrative
