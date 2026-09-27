"""add_anchored_vwap must never emit 0 for zero/NaN-volume session-open bars."""
import numpy as np
import pandas as pd

from utils.indicators import add_anchored_vwap


def _bars():
    days = []
    for d in ("2026-09-22", "2026-09-23", "2026-09-24"):
        days.append(pd.date_range(f"{d} 09:15", periods=20, freq="5min", tz="Asia/Kolkata"))
    idx = days[0].append(days[1]).append(days[2])
    close = 2900 + np.arange(len(idx)) * 0.5
    df = pd.DataFrame({"Open": close - 1, "High": close + 2, "Low": close - 2,
                       "Close": close, "Volume": 10_000.0}, index=idx)
    df.iloc[0, df.columns.get_loc("Volume")] = 0.0        # zero-volume open
    df.iloc[20, df.columns.get_loc("Volume")] = np.nan    # NaN-volume open
    df.iloc[40:42, df.columns.get_loc("Volume")] = 0.0    # two zero bars
    return df


def test_no_zero_avwap_on_bad_volume_opens():
    out = add_anchored_vwap(_bars())
    for col in ("AVWAP", "AVWAP_SD1_Upper", "AVWAP_SD1_Lower", "AVWAP_SD2_Upper", "AVWAP_SD2_Lower"):
        assert not (out[col] == 0).any(), col
    # undefined opening bars are NaN, not 0
    assert out["AVWAP"].iloc[[0, 20, 40, 41]].isna().all()
    # rest of each session is defined and near price (NaN volume didn't poison the day)
    valid = out["AVWAP"].drop(out.index[[0, 20, 40, 41]])
    assert valid.notna().all()
    assert ((valid - out["Close"].loc[valid.index]).abs() < 20).all()
