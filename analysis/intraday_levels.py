"""
analysis/intraday_levels.py - reference levels and session stats for the
digest picks' intraday messages (watchlist, opening update, ±2% moves).

Pure (no Streamlit, no I/O).

daily_levels():  from completed daily bars —
    y_close   yesterday's close
    atr       average daily range (Wilder ATR-14), "a typical day's move"
    up        y_close + ½ ATR   (upside level)
    down      y_close − ½ ATR   (downside level)
    next_up   up + 1.5 ATR      (next level above the upside level)

session_stats(): from today's intraday bars (naive IST index) —
    open, high, low, last, and the opening range (first 15 min, 09:15–09:30).
"""
from __future__ import annotations

import datetime
from typing import Optional

import pandas as pd

NEXT_LEVEL_ATR = 1.5
_OPENING_RANGE_END = datetime.time(9, 30)


def daily_levels(df: pd.DataFrame) -> Optional[dict]:
    """None when there are fewer than 20 bars or the columns are missing."""
    if df is None or df.empty or len(df) < 20 or not {"High", "Low", "Close"} <= set(df.columns):
        return None
    high, low, close = df["High"], df["Low"], df["Close"]
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = float(tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1])
    y_close = float(close.iloc[-1])
    up, down = y_close + 0.5 * atr, y_close - 0.5 * atr
    return {"y_close": y_close, "atr": atr, "up": up, "down": down,
            "next_up": up + NEXT_LEVEL_ATR * atr}


def session_stats(bars: pd.DataFrame, day: Optional[datetime.date] = None) -> Optional[dict]:
    """Stats for `day` (default: the last date in `bars`). None if no bars that day."""
    if bars is None or bars.empty or not {"Open", "High", "Low", "Close"} <= set(bars.columns):
        return None
    idx = pd.to_datetime(bars.index)
    day = day or idx[-1].date()
    today = bars[idx.date == day]
    if today.empty:
        return None
    t_idx = pd.to_datetime(today.index)
    opening = today[t_idx.time < _OPENING_RANGE_END]
    return {
        "open": float(today["Open"].iloc[0]),
        "high": float(today["High"].max()),
        "low":  float(today["Low"].min()),
        "last": float(today["Close"].iloc[-1]),
        "or_high": float(opening["High"].max()) if not opening.empty else None,
        "or_low":  float(opening["Low"].min()) if not opening.empty else None,
    }


def position_vs_levels(price: float, lv: dict) -> str:
    """Plain-language position of `price` against the upside/downside levels."""
    if price >= lv["next_up"]:
        return f"above its next level ₹{lv['next_up']:,.2f}"
    if price > lv["up"]:
        return f"above its upside level ₹{lv['up']:,.2f}"
    if price < lv["down"]:
        return f"below its downside level ₹{lv['down']:,.2f}"
    return f"inside its normal range (₹{lv['down']:,.2f}–₹{lv['up']:,.2f})"
