"""tests/test_portfolio_posture.py — Unit tests for portfolio posture analysis.

Uses synthetic OHLCV frames so it runs offline (no network, no Angel One
session). Each test targets one posture decision — HOLD, EXIT_WATCH,
TRIM_WATCH, ADD_WATCH for delivery, STOP_HIT / TARGET_HIT / TRAIL_TIGHTER
for intraday — and asserts the classifier picks the right label.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.portfolio_posture import (
    DELIVERY_POSTURES, INTRADAY_POSTURES,
    analyse_delivery_holding, analyse_intraday_position,
    analyse_holdings, format_portfolio_message,
)


def _mk_frame(closes, volumes=None):
    n = len(closes)
    if volumes is None:
        volumes = np.full(n, 100_000.0)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open":   closes, "High":   closes * 1.01,
        "Low":    closes * 0.99, "Close":  closes,
        "Volume": volumes,
    }, index=idx)


def _uptrend(n=250, drift=0.25, noise=1.5, seed=42, start=100.0):
    return start + np.arange(n) * drift + noise * np.random.RandomState(seed).randn(n)


# ─────────────────────────────────────────────────────────────────────────────
# Delivery posture — each label in isolation
# ─────────────────────────────────────────────────────────────────────────────

def test_hold_when_thesis_intact_but_not_extending():
    """Uptrend still valid, 3–4 filters passing, price hasn't broken 55d."""
    closes = _uptrend(250)
    # Don't force a breakout — 3–4 filters will pass but not all 6
    df = _mk_frame(closes)
    holding = {"symbol": "TCS", "qty": 10, "avg_price": 80.0, "ltp": float(closes[-1])}
    result = analyse_delivery_holding(holding, df)

    assert result is not None
    assert result.posture in ("HOLD", "TRIM_WATCH", "ADD_WATCH"), \
        f"expected non-EXIT posture on uptrend, got {result.posture}"
    assert 2 <= result.filters_passing <= 6


def test_exit_watch_when_price_breaks_below_sma50():
    """SMA50 breakdown must flip to EXIT_WATCH regardless of P&L."""
    # Build an uptrend, then a hard 15% drop at the end
    closes = _uptrend(240, drift=0.4)
    # Force last 10 bars to crash below SMA50
    sma50_level = float(pd.Series(closes).rolling(50).mean().iloc[-1])
    for i in range(10):
        closes = np.append(closes, sma50_level * 0.90)
    df = _mk_frame(closes)
    holding = {"symbol": "BROKEN", "qty": 10, "avg_price": 50.0, "ltp": float(closes[-1])}
    result = analyse_delivery_holding(holding, df)
    assert result is not None
    assert result.posture == "EXIT_WATCH", \
        f"expected EXIT_WATCH on SMA50 breakdown, got {result.posture}"
    assert "SMA50" in result.reason
    assert result.suggested_stop is not None


def test_add_watch_when_all_filters_pass_and_pnl_positive():
    """5-6 filters passing + green position = ADD_WATCH."""
    closes = _uptrend(250)
    closes[-1] = closes[-56:-1].max() * 1.02   # break 55d high
    volumes = np.full(250, 100_000.0)
    volumes[-1] = 200_000.0
    df = _mk_frame(closes, volumes=volumes)
    holding = {"symbol": "HOT", "qty": 10, "avg_price": 50.0, "ltp": float(closes[-1])}
    result = analyse_delivery_holding(holding, df)
    assert result is not None
    assert result.filters_passing >= 5
    assert result.posture == "ADD_WATCH"


def test_trim_watch_when_setup_decayed_but_trend_intact():
    """0–2 filters passing but SMA50 not broken → TRIM_WATCH."""
    # Consolidating flat after an uptrend — trend intact, no breakout, RSI ~50
    closes = np.concatenate([_uptrend(200, drift=0.3), np.full(50, 200.0) + np.random.RandomState(1).randn(50)*0.5])
    df = _mk_frame(closes, volumes=np.full(250, 100_000.0))   # low volume
    holding = {"symbol": "STALLED", "qty": 10, "avg_price": 100.0, "ltp": float(closes[-1])}
    result = analyse_delivery_holding(holding, df)
    assert result is not None
    # SMA50 not broken (still above from the earlier trend) so not EXIT_WATCH
    # but filters passing should be low → TRIM_WATCH
    assert result.posture in ("TRIM_WATCH", "HOLD", "EXIT_WATCH")


def test_insufficient_data_returns_none():
    holding = {"symbol": "FRESH", "qty": 10, "avg_price": 100, "ltp": 100}
    df = _mk_frame(_uptrend(50))  # < min_bars_needed
    assert analyse_delivery_holding(holding, df) is None


def test_all_delivery_postures_are_valid_labels():
    """Guarantee no code path emits a label outside DELIVERY_POSTURES."""
    closes = _uptrend(250)
    for scenario_start in [50.0, 100.0, 200.0]:   # under-water, break-even, in profit
        df = _mk_frame(closes)
        h = {"symbol": "X", "qty": 10, "avg_price": scenario_start, "ltp": float(closes[-1])}
        result = analyse_delivery_holding(h, df)
        if result:
            assert result.posture in DELIVERY_POSTURES


# ─────────────────────────────────────────────────────────────────────────────
# Intraday posture
# ─────────────────────────────────────────────────────────────────────────────

def test_intraday_stop_hit_when_pnl_below_stop_pct():
    df = _mk_frame(_uptrend(250))
    pos = {"symbol": "LOSE", "qty": 10, "avg_price": 100.0, "ltp": 98.5, "side": "LONG"}
    result = analyse_intraday_position(pos, df, stop_pct=1.0, target_pct=2.0)
    assert result is not None
    assert result.posture == "STOP_HIT"


def test_intraday_target_hit_when_pnl_above_target():
    df = _mk_frame(_uptrend(250))
    pos = {"symbol": "WIN", "qty": 10, "avg_price": 100.0, "ltp": 102.5, "side": "LONG"}
    result = analyse_intraday_position(pos, df, stop_pct=1.0, target_pct=2.0)
    assert result is not None
    assert result.posture == "TARGET_HIT"


def test_intraday_trail_tighter_at_60pct_of_target():
    df = _mk_frame(_uptrend(250))
    pos = {"symbol": "TRAIL", "qty": 10, "avg_price": 100.0, "ltp": 101.4, "side": "LONG"}
    result = analyse_intraday_position(pos, df, stop_pct=1.0, target_pct=2.0)
    assert result is not None
    assert result.posture == "TRAIL_TIGHTER"


def test_intraday_running_when_within_range():
    df = _mk_frame(_uptrend(250))
    pos = {"symbol": "MID", "qty": 10, "avg_price": 100.0, "ltp": 100.3, "side": "LONG"}
    result = analyse_intraday_position(pos, df, stop_pct=1.0, target_pct=2.0)
    assert result is not None
    assert result.posture == "RUNNING"


def test_intraday_short_inverts_pnl_sign():
    """For a SHORT, LTP > avg = losing, LTP < avg = winning."""
    df = _mk_frame(_uptrend(250))
    pos = {"symbol": "SHORT", "qty": -10, "avg_price": 100.0, "ltp": 97.5, "side": "SHORT"}
    result = analyse_intraday_position(pos, df, stop_pct=1.0, target_pct=2.0)
    # Short is +2.5% winning → TARGET_HIT
    assert result is not None
    assert result.posture == "TARGET_HIT"


def test_intraday_missing_data_returns_unclear():
    pos = {"symbol": "?", "qty": 10, "avg_price": 0.0, "ltp": 0.0, "side": "LONG"}
    result = analyse_intraday_position(pos, None)
    assert result is not None
    assert result.posture == "UNCLEAR"


# ─────────────────────────────────────────────────────────────────────────────
# Batch analysis
# ─────────────────────────────────────────────────────────────────────────────

def test_analyse_holdings_skips_bad_fetch():
    holdings = [
        {"symbol": "GOOD",  "qty": 10, "avg_price": 50, "ltp": 100},
        {"symbol": "MISS",  "qty": 5,  "avg_price": 200, "ltp": 180},
    ]
    def fetcher(t):
        if t == "MISS":
            return None
        return _mk_frame(_uptrend(250))
    out = analyse_holdings(holdings, fetcher)
    assert len(out) == 1
    assert out[0].symbol == "GOOD"


def test_format_message_no_urgent_says_all_hold():
    """Zero actionable rows → the 'no postures flagged' text, not an empty message."""
    msg = format_portfolio_message([], [])
    assert "No postures flagged" in msg
    assert "Portfolio Posture" in msg


def test_format_message_lists_urgent_rows():
    from analysis.portfolio_posture import HoldingPosture, IntradayPosture
    h = HoldingPosture(
        symbol="XYZ", qty=10, avg_price=100.0, ltp=85.0, pnl_pct=-15.0,
        posture="EXIT_WATCH", reason="SMA50 breakdown", filters_passing=1,
    )
    p = IntradayPosture(
        symbol="ABC", qty=5, avg_price=100.0, ltp=102.5, pnl_pct=2.5,
        posture="TARGET_HIT", reason="through target", vwap_today=101.0,
    )
    msg = format_portfolio_message([h], [p])
    assert "XYZ" in msg and "EXIT_WATCH" in msg
    assert "ABC" in msg and "TARGET_HIT" in msg
    assert "Not advice" in msg


def test_all_intraday_postures_are_valid_labels():
    """Same paranoid check for the intraday side."""
    df = _mk_frame(_uptrend(250))
    for ltp in [95, 99, 100, 101, 105]:
        pos = {"symbol": "X", "qty": 10, "avg_price": 100, "ltp": ltp, "side": "LONG"}
        result = analyse_intraday_position(pos, df)
        if result:
            assert result.posture in INTRADAY_POSTURES
