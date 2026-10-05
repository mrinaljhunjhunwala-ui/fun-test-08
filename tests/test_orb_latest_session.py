"""Opening Range Breakout must use only the latest session (replica-test S2,
2026-10-04).

The intraday scan fetches 3 days of bars; compute_orb() masked 09:15-09:29 on
the whole frame, so the "opening range" pooled every day's open. Reproduced:
day-1 range 80-120, day-2 range 99-101 -> returned 120/80. check_orb_signal()
had the same pooling, and its "post-ORB" bars included earlier days, so early
in a session the "current bar" could be yesterday's close.
"""
import numpy as np
import pandas as pd
import pytest

from trading.intraday_signals import check_orb_signal, compute_orb


def _session(day, lo, hi, until="15:25", vol=1000.0, close=None):
    idx = pd.date_range(f"{day} 09:15", f"{day} {until}", freq="5min")
    lo, hi = float(lo), float(hi)
    mid = (lo + hi) / 2
    c = mid if close is None else close
    return pd.DataFrame({"Open": mid, "High": hi, "Low": lo, "Close": c, "Volume": vol}, index=idx)


def test_orb_ignores_earlier_sessions():
    df = pd.concat([_session("2026-10-01", 80, 120), _session("2026-10-05", 99, 101)])
    orb = compute_orb(df)
    assert (orb["orb_high"], orb["orb_low"]) == (101.0, 99.0)
    assert orb["orb_bars"] == 3          # 09:15, 09:20, 09:25


@pytest.mark.parametrize("minutes,bars", [(15, 3), (30, 6), (60, 12)])
def test_orb_windows_including_an_hour(minutes, bars):
    # dtime(9, 15 + 60 - 1) used to raise (minute 74) and return NaN.
    orb = compute_orb(_session("2026-10-05", 99, 101), orb_minutes=minutes)
    assert orb["orb_bars"] == bars and not np.isnan(orb["orb_high"])


def test_no_signal_from_yesterdays_bars_before_todays_range_completes():
    # Yesterday's last bar closed far above every opening range on heavy
    # volume; today only has the first two 5-min bars (range still forming),
    # so there is no post-ORB bar today and nothing to signal.
    yday = _session("2026-10-01", 80, 120)
    yday.loc[yday.index[-1], ["High", "Close", "Volume"]] = [150.0, 150.0, 50_000.0]
    today = _session("2026-10-05", 99, 101, until="09:20")
    assert check_orb_signal(pd.concat([yday, today])) is None


def test_breakout_measured_against_todays_range_and_volume():
    yday = _session("2026-10-01", 80, 120, vol=100_000.0)
    today = _session("2026-10-05", 99, 101, until="09:40", vol=1000.0)
    today.loc[today.index[-1], ["Close", "High", "Volume"]] = [103.0, 103.5, 5000.0]
    sig = check_orb_signal(pd.concat([yday, today]))
    assert sig is not None and sig["orb_high"] == 101.0 and sig["orb_low"] == 99.0
    assert sig["vol_ratio"] == 5.0
