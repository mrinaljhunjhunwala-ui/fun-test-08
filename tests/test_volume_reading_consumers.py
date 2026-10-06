"""Volume consumers read the same session-aware number as the composite score.

Audit 2026-10-03 follow-up 2 to #281: the Analyze "Vol Ratio" tile, the trade
checklist, the deep-confirmation "Volume supportive" check and the
signals.py breakout / oversold gates all read today's partial daily bar
during market hours, so the tile could say 0.6x while the narrative said
1.5x, and the volume gates rejected almost every stock in the morning.
They now go through analysis.score.volume_reading(); on completed
(historical) bars nothing changes, so backtests are untouched.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

import analysis.score as S
import trading.signals as SIG
from dashboard.shared import volume_display as VD
from utils.indicators import add_all_indicators


def _frame(end, n=120, last_vol=None, prev_vol=None, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=end, periods=n)
    c = 100 * np.cumprod(1 + rng.normal(0.0005, 0.01, n))
    o = c * (1 + rng.normal(0, 0.003, n))
    df = pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.004,
                       "Low": np.minimum(o, c) * 0.996, "Close": c,
                       "Volume": np.full(n, 100_000.0)}, index=idx)
    if prev_vol is not None:
        df.iloc[-2, df.columns.get_loc("Volume")] = prev_vol
    if last_vol is not None:
        df.iloc[-1, df.columns.get_loc("Volume")] = last_vol
    return add_all_indicators(df)


def _clock(monkeypatch, h, m, day=(2026, 10, 5)):
    monkeypatch.setattr(S, "_now_ist_naive", lambda: dt.datetime(*day, h, m))


def test_signals_ratio_unchanged_on_historical_bars(monkeypatch):
    _clock(monkeypatch, 11, 30)
    df = _frame("2026-09-30", last_vol=250_000.0)          # completed, past bar
    assert SIG._session_volume_ratio(df) == pytest.approx(df["Volume_Ratio"].iloc[-1])


def test_signals_ratio_nan_preserved_on_historical_bars(monkeypatch):
    _clock(monkeypatch, 16, 0)
    df = _frame("2026-09-30")
    df.iloc[-1, df.columns.get_loc("Volume_Ratio")] = np.nan
    assert np.isnan(SIG._session_volume_ratio(df))


def test_signals_use_previous_session_early_in_the_day(monkeypatch):
    _clock(monkeypatch, 9, 45)
    df = _frame("2026-10-05", last_vol=8_000.0, prev_vol=200_000.0)
    r = S.volume_reading(df)
    assert r["basis"] == "previous_session"
    assert SIG._session_volume_ratio(df) == pytest.approx(r["ratio"])
    assert SIG._session_volume_ratio(df) > 1.5            # not the partial 0.08x


def test_signals_use_projection_later_in_the_day(monkeypatch):
    _clock(monkeypatch, 13, 0)
    df = _frame("2026-10-05", last_vol=60_000.0)
    r = S.volume_reading(df)
    assert r["basis"] == "projected"
    assert SIG._session_volume_ratio(df) == pytest.approx(r["ratio"])
    assert SIG._session_volume_ratio(df) > df["Volume_Ratio"].iloc[-1]


def test_tile_labels_basis_and_volumes():
    v, note = VD.vol_ratio_tile({"ratio": 1.62, "ratio_raw": 0.55, "volume": 3_200_000,
                                 "baseline": 2_000_000, "basis": "projected", "session_minutes": 135})
    assert v == "1.62x"
    assert note == "High volume · on pace (11:30) · 32.0L vs 20.0L avg"
    v, note = VD.vol_ratio_tile({"ratio": 0.9, "ratio_raw": 0.9, "basis": "previous_session"})
    assert (v, note) == ("0.90x", "Normal · last session")
    v, note = VD.vol_ratio_tile({"ratio": 1.1, "ratio_raw": 1.1, "basis": "latest", "volume": None})
    assert (v, note) == ("1.10x", "Normal")
    # unknown volume: volume_reading's neutral 1.0 must not read as a measurement
    assert VD.vol_ratio_tile({"ratio": 1.0, "ratio_raw": None, "basis": "latest"}) == ("–", "Volume unavailable")


def test_reading_never_raises_on_bad_frames():
    assert VD.reading(pd.DataFrame())["basis"] == "latest"
    r = VD.reading(pd.DataFrame({"Volume_Ratio": [1.3]}))   # no OHLC: unreadable
    assert r["basis"] == "latest" and r["ratio"] is not None


def test_signals_unknown_live_volume_stays_nan(monkeypatch):
    # volume_reading() returns ratio 1.0 / ratio_raw None when Volume_Ratio is
    # missing; the signal gates must still see "unknown", not "normal".
    _clock(monkeypatch, 13, 0)
    df = _frame("2026-10-05", last_vol=60_000.0)
    df.iloc[-1, df.columns.get_loc("Volume_Ratio")] = np.nan
    assert S.volume_reading(df)["ratio_raw"] is None
    assert np.isnan(SIG._session_volume_ratio(df))
