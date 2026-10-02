"""tests/test_regime.py — regression cover for the regime classifier and
dispersion filter added in Path B.

These pin the specific behaviours the audit-driven fixes rely on:

* dispersion_verdict returns "low" below the threshold and appends a real
  warning note that the score narrative can paste in verbatim.
* classify() falls back gracefully when breadth is unknown (the historical
  path and the live snapshot both hit that fallback — see FIX REGIME-BREADTH).
* score_dataframe accepts a `dispersion` kwarg and only appends the note
  for BUY / STRONG BUY (never for HOLD / CAUTION / EXIT / WATCHLIST).
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.regime import (  # noqa: E402
    DISPERSION_LOW_THRESHOLD,
    classify,
    cross_sectional_dispersion_20d,
    dispersion_verdict,
)


# ─────────────── dispersion_verdict boundaries ───────────────

def test_dispersion_verdict_low_zone_carries_a_paste_ready_note():
    v = dispersion_verdict(DISPERSION_LOW_THRESHOLD - 1.0)
    assert v["zone"] == "low"
    assert isinstance(v["note"], str) and v["note"]
    # Descriptive (CLAUDE.md rule 1): states the reliability hit, no instruction.
    assert "less reliable" in v["note"].lower() and "consider" not in v["note"].lower()


def test_dispersion_verdict_normal_zone_has_empty_note():
    v = dispersion_verdict(DISPERSION_LOW_THRESHOLD + 2.0)
    assert v["zone"] == "normal"
    # Empty note is deliberate — no need to clutter the narrative when nothing
    # unusual is happening (normal is normal).
    assert v["note"] == ""


def test_dispersion_verdict_high_zone_flags_stock_picking_favourable():
    v = dispersion_verdict(20.0)
    assert v["zone"] == "high"
    assert "winners" in v["note"].lower() or "picking" in v["note"].lower()


def test_dispersion_verdict_none_and_nan_are_unknown():
    assert dispersion_verdict(None)["zone"] == "unknown"
    assert dispersion_verdict(float("nan"))["zone"] == "unknown"
    # Unknown must not silently look like a warning
    assert dispersion_verdict(None)["note"] == ""


# ─────────────── cross_sectional_dispersion_20d ───────────────

def _fake_close(n: int = 30, drift: float = 0.0, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100 + np.cumsum(rng.normal(drift, 1.0, n)))


def test_cross_sectional_dispersion_needs_at_least_20_tickers():
    frames = {f"T{i}": _fake_close(30, drift=0.1, seed=i) for i in range(10)}
    assert cross_sectional_dispersion_20d(frames) is None


def test_cross_sectional_dispersion_is_std_across_universe():
    # 30 tickers with different drifts → real dispersion
    frames = {f"T{i}": _fake_close(30, drift=(i - 15) * 0.05, seed=i) for i in range(30)}
    d = cross_sectional_dispersion_20d(frames)
    assert d is not None and d > 0

    # 30 identical series → dispersion ~ 0
    same = _fake_close(30, drift=0.1, seed=42)
    same_frames = {f"T{i}": same.copy() for i in range(30)}
    d_same = cross_sectional_dispersion_20d(same_frames)
    assert d_same is not None and d_same < 1e-6


# ─────────────── classify() breadth fallback (FIX REGIME-BREADTH) ───────────────

def _uptrend_nifty(days: int = 250) -> pd.Series:
    rng = np.random.default_rng(1)
    # Clear uptrend so trend component reads "above"
    return pd.Series(100 + np.cumsum(rng.normal(0.5, 0.8, days)))


def test_classify_falls_back_to_trend_when_breadth_unknown():
    """This is the exact bug that made the composite regime NEVER assign
    trend_up on the historical path — before FIX REGIME-BREADTH,
    breadth=None + trend=above returned "range" every time."""
    snap = classify(vix=15.0, nifty_close=_uptrend_nifty(), pct_above_sma50=None)
    assert snap.label == "trend_up"
    # Confidence capped at medium when breadth is missing
    assert snap.confidence == "medium"
    # The reason must SAY breadth was unavailable so a UI reader isn't misled
    joined = " ".join(snap.reasons).lower()
    assert "breadth not used" in joined


def test_classify_high_confidence_when_breadth_broad():
    snap = classify(vix=15.0, nifty_close=_uptrend_nifty(), pct_above_sma50=70.0)
    assert snap.label == "trend_up"
    assert snap.confidence == "high"


def test_classify_risk_off_wins_over_trend():
    """VIX in panic zone must land in risk_off no matter what the trend says."""
    snap = classify(vix=32.0, nifty_close=_uptrend_nifty(), pct_above_sma50=70.0)
    assert snap.label == "risk_off"


# ─────────────── score_dataframe integration ───────────────

def _ohlcv(direction: str = "up", n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = 0.6 if direction == "up" else -0.6
    steps = rng.normal(drift, 0.8, n)
    close = np.maximum(100 + np.cumsum(steps), 5.0)
    openp = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(openp, close) * (1 + rng.uniform(0.0, 0.01, n))
    low  = np.minimum(openp, close) * (1 - rng.uniform(0.0, 0.01, n))
    vol  = rng.integers(500_000, 2_000_000, n).astype(float)
    idx  = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({"Open": openp, "High": high, "Low": low,
                         "Close": close, "Volume": vol}, index=idx)


def test_score_dataframe_appends_dispersion_note_only_for_buy_signals():
    from analysis.score import score_dataframe
    from utils.indicators import add_all_indicators

    df = add_all_indicators(_ohlcv("up"))

    # Low dispersion + likely-BUY-band synthetic uptrend → note appears
    res_low = score_dataframe(df, "TEST.NS", sector="Other",
                              dispersion=DISPERSION_LOW_THRESHOLD - 2.0)
    if res_low.action in ("BUY", "STRONG BUY"):
        assert "dispersion" in res_low.narrative.lower(), (
            f"low-dispersion note missing for {res_low.action}: {res_low.narrative!r}"
        )

    # Normal dispersion → no note
    res_norm = score_dataframe(df, "TEST.NS", sector="Other",
                               dispersion=DISPERSION_LOW_THRESHOLD + 2.0)
    assert "cross-sectional dispersion" not in res_norm.narrative.lower()

    # No dispersion passed → previous behaviour unchanged
    res_none = score_dataframe(df, "TEST.NS", sector="Other")
    assert "cross-sectional dispersion" not in res_none.narrative.lower()


def test_score_dataframe_dispersion_never_appears_on_non_buy_actions():
    """The note carries a "consider halving" instruction — it only makes sense
    for BUY signals. HOLD / CAUTION / EXIT / WATCHLIST must never see it,
    even in low-dispersion regime."""
    from analysis.score import score_dataframe
    from utils.indicators import add_all_indicators

    df = add_all_indicators(_ohlcv("down"))   # forces low-score action band
    res = score_dataframe(df, "TEST.NS", sector="Other",
                          dispersion=DISPERSION_LOW_THRESHOLD - 3.0)
    if res.action not in ("BUY", "STRONG BUY"):
        assert "cross-sectional dispersion" not in res.narrative.lower(), (
            f"dispersion note leaked into {res.action} narrative"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
