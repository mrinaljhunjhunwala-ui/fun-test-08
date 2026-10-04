"""
trading/intraday_signals.py
Intraday signal generation for NSE equities.

Strategies implemented:
    1. Opening Range Breakout (ORB) — #1 intraday strategy in India
    2. VWAP Deviation Entry        — buy/short at extreme AVWAP deviations
    3. Supertrend Flip Signal      — intraday trend change on 5m/15m chart
    4. CPR Breakout                — price breaks above/below CPR with volume

All functions expect an intraday DataFrame from fetch_intraday() (5m/15m bars).
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from datetime import datetime, time as dtime
from typing import Dict, List, Optional

_log = logging.getLogger("trading.intraday_signals")

from utils.indicators import add_all_indicators, add_anchored_vwap


# ─────────────────────────────────────────────────────────────────────────────
# Opening Range Breakout (ORB)
# ─────────────────────────────────────────────────────────────────────────────

def _orb_window(orb_minutes: int) -> tuple:
    """(start, last-bar-start) of the opening range, e.g. 09:15 / 09:29 for 15 min.

    Computed with datetime arithmetic: dtime(9, 15 + orb_minutes - 1) raised
    for any window of 46+ minutes (minute 74 for a 60-min ORB).
    """
    start = datetime(2000, 1, 1, 9, 15)
    return start.time(), (start + pd.Timedelta(minutes=orb_minutes - 1)).time()


def _latest_session(df: pd.DataFrame) -> pd.DataFrame:
    """Bars of the most recent trading date only.

    FIX ORB-DAYS (replica-test S2, 2026-10-04): the intraday scan fetches 3
    days of bars, and the 09:15 mask then pooled every day's opening range.
    """
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df
    last_day = df.index[-1].date()
    return df[df.index.date == last_day]


def compute_orb(df: pd.DataFrame, orb_minutes: int = 15) -> Dict:
    """
    Compute the Opening Range for an intraday DataFrame.

    The Opening Range = first `orb_minutes` of trading (from 09:15 IST).

    Args:
        df          : intraday DataFrame with datetime index (IST), OHLCV
        orb_minutes : duration of opening range in minutes (15 or 30)

    Returns dict:
        {
            "orb_high"     : float,  # High of opening range
            "orb_low"      : float,  # Low of opening range
            "orb_range"    : float,  # orb_high - orb_low
            "orb_range_pct": float,  # range as % of open price
            "narrow"       : bool,   # True if range < 0.5% (low-volatility day)
            "open_price"   : float,  # first bar's open
            "orb_bars"     : int,    # number of bars in the opening range
        }
    """
    if df.empty:
        return {"orb_high": np.nan, "orb_low": np.nan, "orb_range": np.nan,
                "orb_range_pct": np.nan, "narrow": False, "open_price": np.nan, "orb_bars": 0}

    try:
        # Filter to the latest session's opening range window
        df = _latest_session(df)
        start_time, cutoff = _orb_window(orb_minutes)   # e.g. 09:15-09:29 for 15-min ORB

        idx_times = df.index.time
        orb_mask  = (idx_times >= start_time) & (idx_times <= cutoff)
        orb_df    = df[orb_mask]

        if orb_df.empty:
            orb_df = df.head(orb_minutes // 5)   # fallback: first N bars

        orb_high  = float(orb_df["High"].max())
        orb_low   = float(orb_df["Low"].min())
        open_px   = float(orb_df["Open"].iloc[0])
        orb_range = orb_high - orb_low
        rng_pct   = orb_range / max(open_px, 1) * 100

        return {
            "orb_high":      round(orb_high, 2),
            "orb_low":       round(orb_low, 2),
            "orb_range":     round(orb_range, 2),
            "orb_range_pct": round(rng_pct, 2),
            "narrow":        rng_pct < 0.5,
            "open_price":    round(open_px, 2),
            "orb_bars":      len(orb_df),
        }
    except Exception as e:
        _log.debug("compute_orb: failed to compute opening range, returning NaN sentinel: %s", e)
        return {"orb_high": np.nan, "orb_low": np.nan, "orb_range": np.nan,
                "orb_range_pct": np.nan, "narrow": False, "open_price": np.nan, "orb_bars": 0}


def check_orb_signal(
    df:          pd.DataFrame,
    orb_minutes: int   = 15,
    vol_mult:    float = 1.5,    # volume must be > vol_mult × ORB avg
) -> Optional[Dict]:
    """
    Check for an ORB breakout signal on the most recent bar.

    Entry conditions (BUY):
        • Price breaks above ORB High (current bar's Close > orb_high)
        • Volume on breakout bar > vol_mult × avg ORB volume

    Entry conditions (SHORT):
        • Price breaks below ORB Low
        • Volume on breakout bar > vol_mult × avg ORB volume

    Stop-loss: opposite side of ORB
    Target   : ORB range projected from the breakout level (1:1.5 R:R default)

    Returns signal dict or None.
    """
    orb = compute_orb(df, orb_minutes)
    if any(np.isnan(v) for v in [orb["orb_high"], orb["orb_low"]]):
        return None

    orb_high = orb["orb_high"]
    orb_low  = orb["orb_low"]
    orb_rng  = orb["orb_range"]

    # Need data after the ORB window — in the SAME session; earlier days'
    # afternoon bars would otherwise pose as today's breakout bar.
    df = _latest_session(df)
    start_time, cutoff = _orb_window(orb_minutes)
    try:
        post_mask = df.index.time > cutoff
        post_df   = df[post_mask]
    except Exception as e:
        _log.debug("check_orb_signal: time-based post-ORB slice failed, using positional fallback: %s", e)
        post_df = df.tail(len(df) - orb["orb_bars"])

    if post_df.empty:
        return None

    cur      = post_df.iloc[-1]
    price    = float(cur["Close"])
    vol      = float(cur.get("Volume", 0))

    # Volume filter: compare vs avg volume during ORB
    try:
        orb_mask    = (df.index.time >= start_time) & (df.index.time <= cutoff)
        orb_avg_vol = float(df[orb_mask]["Volume"].mean())
    except Exception as e:
        _log.debug("check_orb_signal: ORB volume calc failed, disabling volume check: %s", e)
        orb_avg_vol = vol / vol_mult   # disable volume check

    vol_ok = vol >= vol_mult * max(orb_avg_vol, 1)

    # BUY signal
    if price > orb_high and vol_ok:
        sl = orb_low
        tp = orb_high + 1.5 * orb_rng
        rr = (tp - price) / max(price - sl, 0.01)
        return {
            "action":       "BUY",
            "screen":       "ORB_Breakout",
            "price":        round(price, 2),
            "sl":           round(sl, 2),
            "tp":           round(tp, 2),
            "rr_ratio":     round(rr, 2),
            "orb_high":     orb_high,
            "orb_low":      orb_low,
            "orb_range_pct": orb["orb_range_pct"],
            "vol_ratio":    round(vol / max(orb_avg_vol, 1), 2),
            "reason":       (f"ORB breakout above {orb_high:.2f} | "
                             f"ORB range={orb['orb_range_pct']:.1f}% | Vol={vol/max(orb_avg_vol,1):.1f}x"),
            "timestamp":    datetime.now().isoformat(),
        }

    # SHORT signal
    if price < orb_low and vol_ok:
        sl = orb_high
        tp = orb_low - 1.5 * orb_rng
        rr = (price - tp) / max(sl - price, 0.01)
        return {
            "action":       "SHORT",
            "screen":       "ORB_Breakdown",
            "price":        round(price, 2),
            "sl":           round(sl, 2),
            "tp":           round(tp, 2),
            "rr_ratio":     round(rr, 2),
            "orb_high":     orb_high,
            "orb_low":      orb_low,
            "orb_range_pct": orb["orb_range_pct"],
            "vol_ratio":    round(vol / max(orb_avg_vol, 1), 2),
            "reason":       (f"ORB breakdown below {orb_low:.2f} | "
                             f"ORB range={orb['orb_range_pct']:.1f}% | Vol={vol/max(orb_avg_vol,1):.1f}x"),
            "timestamp":    datetime.now().isoformat(),
        }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# VWAP Deviation Entry
# ─────────────────────────────────────────────────────────────────────────────

def check_vwap_entry(
    df:           pd.DataFrame,
    min_deviation: float = 1.5,    # % away from VWAP to consider extreme
) -> Optional[Dict]:
    """
    Entry when price deviates significantly from intraday VWAP then shows reversal.

    BUY  : price > min_deviation% BELOW VWAP + current bar shows bullish close
    SHORT: price > min_deviation% ABOVE VWAP + current bar shows bearish close

    Requires add_anchored_vwap() to have been called on the DataFrame.
    """
    if "AVWAP" not in df.columns:
        df = add_anchored_vwap(df)

    cur   = df.iloc[-1]
    price = float(cur["Close"])
    avwap = float(cur.get("AVWAP", np.nan))
    atr   = float(cur.get("ATR", price * 0.003))   # ~0.3% fallback

    if pd.isna(avwap) or avwap <= 0:
        return None

    dev_pct = (price - avwap) / avwap * 100

    # BUY: price is deep below VWAP, last bar is bullish (close > open)
    if dev_pct <= -min_deviation:
        is_bullish = float(cur["Close"]) > float(cur.get("Open", cur["Close"]))
        if is_bullish:
            sl = price - 2 * atr
            tp = avwap + atr   # target: VWAP + 1 ATR
            rr = (tp - price) / max(price - sl, 0.01)
            return {
                "action":      "BUY",
                "screen":      "VWAP_Reversal",
                "price":       round(price, 2),
                "sl":          round(sl, 2),
                "tp":          round(tp, 2),
                "rr_ratio":    round(rr, 2),
                "avwap":       round(avwap, 2),
                "dev_pct":     round(dev_pct, 2),
                "reason":      f"Price {dev_pct:.1f}% below AVWAP ({avwap:.2f}) — mean-reversion long",
                "timestamp":   datetime.now().isoformat(),
            }

    # SHORT: price is extended above VWAP, last bar is bearish
    if dev_pct >= min_deviation:
        is_bearish = float(cur["Close"]) < float(cur.get("Open", cur["Close"]))
        if is_bearish:
            sl = price + 2 * atr
            tp = avwap - atr
            rr = (price - tp) / max(sl - price, 0.01)
            return {
                "action":      "SHORT",
                "screen":      "VWAP_Reversal",
                "price":       round(price, 2),
                "sl":          round(sl, 2),
                "tp":          round(tp, 2),
                "rr_ratio":    round(rr, 2),
                "avwap":       round(avwap, 2),
                "dev_pct":     round(dev_pct, 2),
                "reason":      f"Price +{dev_pct:.1f}% above AVWAP ({avwap:.2f}) — mean-reversion short",
                "timestamp":   datetime.now().isoformat(),
            }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Supertrend Flip  (intraday)
# ─────────────────────────────────────────────────────────────────────────────

def check_supertrend_flip(df: pd.DataFrame) -> Optional[Dict]:
    """
    Signal on Supertrend direction change (intraday).

    Requires add_supertrend() (or add_all_indicators()) on the DataFrame.
    """
    if "ST_Signal" not in df.columns or "Supertrend" not in df.columns:
        return None

    cur       = df.iloc[-1]
    signal    = int(cur.get("ST_Signal", 0))
    price     = float(cur["Close"])
    st_val    = float(cur.get("Supertrend", np.nan))
    atr       = float(cur.get("ATR", price * 0.003))

    if signal == 1:   # just flipped bullish
        sl = st_val if not np.isnan(st_val) else price - 2 * atr
        tp = price + 2 * (price - sl)
        rr = (tp - price) / max(price - sl, 0.01)
        return {
            "action":    "BUY",
            "screen":    "Supertrend_Flip",
            "price":     round(price, 2),
            "sl":        round(sl, 2),
            "tp":        round(tp, 2),
            "rr_ratio":  round(rr, 2),
            "st_value":  round(st_val, 2) if not np.isnan(st_val) else None,
            "reason":    f"Supertrend flipped BULLISH | SL at ST={sl:.2f}",
            "timestamp": datetime.now().isoformat(),
        }
    elif signal == -1:   # just flipped bearish
        sl = st_val if not np.isnan(st_val) else price + 2 * atr
        tp = price - 2 * (sl - price)
        rr = (price - tp) / max(sl - price, 0.01)
        return {
            "action":    "SHORT",
            "screen":    "Supertrend_Flip",
            "price":     round(price, 2),
            "sl":        round(sl, 2),
            "tp":        round(tp, 2),
            "rr_ratio":  round(rr, 2),
            "st_value":  round(st_val, 2) if not np.isnan(st_val) else None,
            "reason":    f"Supertrend flipped BEARISH | SL at ST={sl:.2f}",
            "timestamp": datetime.now().isoformat(),
        }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Full intraday scan — all signals
# ─────────────────────────────────────────────────────────────────────────────

def scan_intraday(
    ticker:      str,
    interval:    str = "5m",
    orb_minutes: int = 15,
) -> Dict:
    """
    Run all intraday signal checks for a single ticker.

    Fetches intraday data, computes indicators, runs all 3 checks.

    Returns:
    {
        "ticker"    : str,
        "interval"  : str,
        "orb"       : dict,           # ORB levels
        "signals"   : [dict, ...],    # list of fired signals (may be empty)
        "price"     : float,
        "avwap"     : float,
        "st_dir"    : 1 | -1,
        "cpr_zone"  : str,
    }
    """
    from data.fetcher import fetch_intraday

    try:
        df = fetch_intraday(ticker, interval=interval, days=3)
        if df.empty or len(df) < 10:
            return {"ticker": ticker, "interval": interval, "signals": [],
                    "error": "Insufficient intraday data"}

        # Add all indicators + anchored VWAP
        from utils.indicators import add_all_indicators
        df = add_all_indicators(df)
        df = add_anchored_vwap(df)

        price   = float(df["Close"].iloc[-1])
        avwap   = float(df["AVWAP"].iloc[-1]) if "AVWAP" in df.columns else np.nan
        st_dir  = int(df["ST_Direction"].iloc[-1]) if "ST_Direction" in df.columns else 0
        cpr_z   = str(df["Price_vs_CPR"].iloc[-1]) if "Price_vs_CPR" in df.columns else "unknown"

        orb     = compute_orb(df, orb_minutes)
        signals = []

        for fn in [
            lambda d: check_orb_signal(d, orb_minutes),
            lambda d: check_vwap_entry(d),
            lambda d: check_supertrend_flip(d),
        ]:
            sig = fn(df)
            if sig:
                sig["ticker"] = ticker
                signals.append(sig)

        return {
            "ticker":   ticker,
            "interval": interval,
            "price":    round(price, 2),
            "avwap":    round(avwap, 2) if not np.isnan(avwap) else None,
            "st_dir":   st_dir,
            "cpr_zone": cpr_z,
            "orb":      orb,
            "signals":  signals,
        }

    except Exception as exc:
        return {"ticker": ticker, "interval": interval, "signals": [],
                "error": str(exc)}
