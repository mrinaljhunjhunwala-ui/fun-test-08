"""
analysis/level_alerts.py - technical level events for the 15-min alert run.

Pure (no Streamlit, no I/O). Given completed daily bars (today's session
excluded) and the current price, report which of these the price has just done:

  * broke above the prior 20-session high   -> "high20"
  * broke below the prior 20-session low    -> "low20"
  * crossed above / below the 50-day SMA    -> "sma50_up" / "sma50_down"

The levels come from the stock's own price history, not from anyone's
opinion of where it should go, so the alert text stays descriptive.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import pandas as pd

LOOKBACK_HIGH_LOW = 20
SMA_WINDOW = 50


@dataclass(frozen=True)
class LevelEvent:
    kind: str        # high20 | low20 | sma50_up | sma50_down
    level: float     # the level that was crossed
    price: float     # current price


def detect_level_events(prior: pd.DataFrame, price: float) -> List[LevelEvent]:
    """Events the current price triggers against `prior` (completed sessions only).

    Returns [] when there's too little history (< SMA_WINDOW sessions) or bad input.
    """
    if prior is None or prior.empty or not price or price <= 0:
        return []
    if len(prior) < SMA_WINDOW or not {"High", "Low", "Close"} <= set(prior.columns):
        return []

    events: List[LevelEvent] = []
    hi = float(prior["High"].iloc[-LOOKBACK_HIGH_LOW:].max())
    lo = float(prior["Low"].iloc[-LOOKBACK_HIGH_LOW:].min())
    if price > hi:
        events.append(LevelEvent("high20", hi, price))
    elif price < lo:
        events.append(LevelEvent("low20", lo, price))

    close = prior["Close"]
    prev_close = float(close.iloc[-1])
    sma_prev = float(close.iloc[-SMA_WINDOW:].mean())
    # Today's SMA treats the current price as today's close. Sitting exactly on
    # the average yesterday (e.g. a flat stock) isn't a side, so it can't cross.
    sma_now = (float(close.iloc[-(SMA_WINDOW - 1):].sum()) + price) / SMA_WINDOW
    if prev_close < sma_prev and price >= sma_now:
        events.append(LevelEvent("sma50_up", sma_now, price))
    elif prev_close > sma_prev and price < sma_now:
        events.append(LevelEvent("sma50_down", sma_now, price))
    return events
