"""
trading/signals.py — Phase 4a  (enhanced with 4-screen approach)
Real-time (delayed) signal scanner for NSE equities.

Fixes applied:
  - check_oversold_bounce: confirmation now OPTIONAL (downgraded to WATCHLIST, not blocked)
  - check_fibonacci_pullback: gracefully returns None when Fib cols absent vs crashing
  - scan_tickers VIX block: changed break → continue so other screens are still tried
  - _VIX_CACHE: now expires after 10 minutes so Streamlit doesn't use stale panic-mode data
  - check_breakout: resolved unresolved git merge-conflict markers that were left in the
    file (syntax error — module could not import); kept the ADX < 20 fakeout-rejection
    check and removed a duplicate "adx" key in the returned dict
"""

from __future__ import annotations

import time
import logging
import warnings
import numpy as np
import pandas as pd
from typing import Any, List, Dict, Optional
from datetime import datetime

_log = logging.getLogger("trading.signals")

# ── 52-week window ───────────────────────────────────────────────────────────
# FIX SIG-52W — check_oversold_bounce() and check_breakout() described their
# reference levels as the "52-week low" / "52-week high" but computed them as
# df["Low"].min() / df["High"].max() over the ENTIRE DataFrame handed in. That
# is only 52 weeks by coincidence, when the caller happens to pass period="1y".
# It doesn't: analysis/score.py fetches "2y" by default, backtest/ and several
# dashboard pages pass 2y–5y, and scan_tickers() itself takes `period` as a
# parameter. On a 5-year frame the "52-week high" became a 5-year high, so
# check_breakout()'s `pct_from_52h <= 3%` gate silently became a near-5-year-
# high gate — a far rarer event, which quietly starves the Breakout screen of
# signals — while check_oversold_bounce()'s `min_pct_above_52wL` floor was
# measured from a 5-year low, letting through names that are nowhere near a
# 52-week base. Both now slice a real 252-trading-day window regardless of how
# much history the caller supplies.
_52W_BARS = 252


def _session_volume_ratio(df: pd.DataFrame) -> float:
    """Volume ratio for the latest session, consistent with the composite score.

    While today's NSE session is in progress the daily bar holds a partial
    day's volume, so the volume gates below rejected nearly every stock in the
    morning. analysis.score.volume_reading() substitutes the previous session
    (before ~10:15 IST) or today's volume projected to a full day. On any
    completed bar — every backtest / historical replay — it is the stored
    Volume_Ratio, NaN preserved, so historical signals are unchanged.
    """
    stored = df.iloc[-1].get("Volume_Ratio", np.nan) if not df.empty else np.nan
    try:
        from analysis.score import volume_reading   # local import: keeps signals import-light
        r = volume_reading(df)
    except Exception as e:
        _log.debug("_session_volume_ratio: volume_reading unavailable: %s", e)
        return stored
    if r.get("basis") == "latest":
        return stored
    return float(r["ratio"])

# FIX WARN1 — narrowed from a blanket `filterwarnings("ignore")` so numpy's
# RuntimeWarnings (invalid value / divide by zero / all-NaN slice) stay visible.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)

from data.fetcher import fetch_single
from utils.indicators import add_all_indicators
from strategies.sector_rotation import SECTORS, compute_sector_scores


# Reverse mapping: ticker → sector name
_TICKER_SECTOR: Dict[str, str] = {
    ticker: sector
    for sector, tickers in SECTORS.items()
    for ticker in tickers
}

# ─────────────────────────────────────────────────────────────────────────────
# India VIX — market sentiment regime
# ─────────────────────────────────────────────────────────────────────────────

_VIX_CACHE:    Optional[Dict] = None
_VIX_CACHE_TS: float          = 0.0
_VIX_TTL:      float          = 600.0   # 10 minutes — prevents stale panic-mode blocking


def get_india_vix_regime() -> Dict:
    """
    Fetch India VIX and return regime info.
    Cached for 10 minutes (was cached forever — caused stale panic-mode blocking in Streamlit).
    """
    global _VIX_CACHE, _VIX_CACHE_TS

    if _VIX_CACHE is not None and (time.time() - _VIX_CACHE_TS) < _VIX_TTL:
        return _VIX_CACHE

    try:
        import json, urllib.request, urllib.parse as _up
        from data.fetcher import _get_yf_crumb
        _opener, _crumb = _get_yf_crumb()
        _cqs = f"&crumb={_up.quote(_crumb)}" if _crumb else ""
        _url = (f"https://query1.finance.yahoo.com/v8/finance/chart/%5EINDIAVIX"
                f"?interval=1d&range=5d&includePrePost=false{_cqs}")
        _req = urllib.request.Request(
            _url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with _opener.open(_req, timeout=8) as _r:
            _d = json.loads(_r.read())
        _closes = _d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        _valid  = [v for v in _closes if v is not None]
        if len(_valid) < 2:
            raise ValueError("No VIX data")

        curr    = float(_valid[-1])
        prev    = float(_valid[-2])
        pct_chg = (curr / prev - 1) * 100

        if curr < 12:   regime = "complacency"
        elif curr < 16: regime = "normal"
        elif curr < 22: regime = "elevated"
        elif curr < 28: regime = "fear"
        else:           regime = "panic"

        _VIX_CACHE = {
            "vix":          round(curr, 2),
            "regime":       regime,
            "allow_buy":    curr <= 28,
            "vix_pct_chg":  round(pct_chg, 2),
        }
        _VIX_CACHE_TS = time.time()

    except Exception as e:
        _log.warning("India VIX fetch failed, defaulting to 'unknown' regime: %s", e)
        _VIX_CACHE    = {"vix": None, "regime": "unknown", "allow_buy": True, "vix_pct_chg": 0.0}
        _VIX_CACHE_TS = time.time()

    return _VIX_CACHE


# ─────────────────────────────────────────────────────────────────────────────
# Helper utilities
# ─────────────────────────────────────────────────────────────────────────────

def calc_trailing_stop(df: pd.DataFrame, entry_price: float, atr_mult: float = 2.0) -> float:
    atr          = float(df["ATR"].iloc[-1]) if "ATR" in df.columns else 0.0
    initial_stop = entry_price - 2.0 * atr
    recent_high  = float(df["Close"].tail(60).max())
    atr_trail    = recent_high - atr_mult * atr
    current_price = float(df["Close"].iloc[-1])
    profit_in_r  = (current_price - entry_price) / (entry_price - initial_stop + 1e-6)
    if profit_in_r < 1.0:
        return initial_stop
    return max(initial_stop, atr_trail)


def find_structure_stop(
    df:         pd.DataFrame,
    lookback:   int   = 20,
    buffer_pct: float = 0.5,
) -> Optional[float]:
    if len(df) < lookback + 2:
        return None
    lows = df["Low"].values[-(lookback + 2):]
    swing_lows = [
        float(lows[i])
        for i in range(1, len(lows) - 1)
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]
    ]
    if not swing_lows:
        return None
    return round(swing_lows[-1] * (1 - buffer_pct / 100), 2)


def kelly_position_size(
    win_rate:     float,
    rr_ratio:     float,
    capital:      float = 100_000.0,
    fraction:     float = 0.5,
    max_risk_pct: float = 2.0,
) -> Dict:
    if not (0 < win_rate < 1):
        raise ValueError(f"win_rate must be in (0, 1), got {win_rate}")
    if rr_ratio <= 0:
        raise ValueError(f"rr_ratio must be positive, got {rr_ratio}")

    q         = 1.0 - win_rate
    raw_kelly = (rr_ratio * win_rate - q) / rr_ratio
    adj_kelly = max(0.0, raw_kelly * fraction)
    kelly_pct = adj_kelly * 100
    risk_pct  = min(kelly_pct, max_risk_pct)
    risk_rs   = capital * risk_pct / 100

    return {
        "kelly_pct": round(kelly_pct, 2),
        "risk_pct":  round(risk_pct,  2),
        "risk_rs":   round(risk_rs,   2),
        "fraction":  fraction,
        "win_rate":  win_rate,
        "rr_ratio":  rr_ratio,
        "notes":     (f"Half-Kelly={kelly_pct:.1f}% → "
                      f"capped at {risk_pct:.1f}% = Rs {risk_rs:,.0f}"),
    }


def shares_from_risk(entry_price: float, stop_price: float, risk_rs: float) -> int:
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return 0
    # FIX RM2: previously forced a minimum of 1 share even when the
    # risk-appropriate quantity rounded to 0 (a wide stop relative to the
    # risk budget) — silently letting a 1-share trade risk more than the
    # caller intended. 0 is the honest answer: this trade doesn't fit the
    # risk budget at this entry/stop distance.
    return int(risk_rs / risk_per_share)


# ─────────────────────────────────────────────────────────────────────────────
# Screen 1: Oversold Bounce
# ─────────────────────────────────────────────────────────────────────────────

def check_oversold_bounce(
    df:                 pd.DataFrame,
    rsi_threshold:      float = 35,
    min_vol_ratio:      float = 0.7,
    min_pct_above_52wL: float = 3.0,
) -> Optional[Dict]:
    """
    RSI oversold bounce.
    FIX: confirmation (pattern/divergence) is now OPTIONAL.
         Without confirmation → signal still fires but action = WATCHLIST.
         Previously: no confirmation → always returned None (no signals ever fired).
    """
    cur   = df.iloc[-1]
    price = float(cur["Close"])

    rsi   = cur.get("RSI", np.nan)
    atr   = cur.get("ATR", np.nan)
    v_rat = _session_volume_ratio(df)   # live session: projected / previous (volume_reading)

    if any(pd.isna(v) for v in [rsi, atr]):
        return None
    if rsi >= rsi_threshold:
        return None
    if not pd.isna(v_rat) and v_rat < min_vol_ratio:
        return None

    low_52w   = float(df["Low"].tail(_52W_BARS).min())   # FIX SIG-52W
    pct_above = (price - low_52w) / max(low_52w, 1) * 100
    if pct_above < min_pct_above_52wL:
        return None

    # Confirmation — optional. Upgrades action to BUY, absent = WATCHLIST
    bull_div = int(cur.get("RSI_Bull_Div",      0))
    bull_eng = int(cur.get("Pat_BullEngulfing",  0))
    hammer   = int(cur.get("Pat_Hammer",         0))
    morn     = int(cur.get("Pat_MorningStar",    0))
    confirmed = bool(bull_div or bull_eng or hammer or morn)

    reason_parts = [f"RSI={rsi:.1f} (oversold)"]
    if bull_div: reason_parts.append("RSI_Bull_Div")
    if bull_eng: reason_parts.append("BullEngulfing")
    if hammer:   reason_parts.append("Hammer")
    if morn:     reason_parts.append("MorningStar")
    if not confirmed:
        reason_parts.append("no_pattern_confirmation")

    sl = price - 2.0 * atr
    tp = price + 3.0 * atr
    return {
        "action":       "BUY" if confirmed else "WATCHLIST",
        "screen":       "Oversold_Bounce",
        "price":        round(price, 2),
        "sl":           round(sl, 2),
        "tp":           round(tp, 2),
        "rsi":          round(rsi, 2),
        "vol_ratio":    round(float(v_rat), 2) if not pd.isna(v_rat) else None,
        "confirmation": confirmed,
        "reason":       " + ".join(reason_parts),
        "timestamp":    datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Screen 2: Momentum Leader
# ─────────────────────────────────────────────────────────────────────────────

def check_momentum_leader(
    df:             pd.DataFrame,
    rsi_lo:         float = 50,
    rsi_hi:         float = 72,
    perf_lookback:  int   = 20,
    min_perf_pct:   float = 2.0,
) -> Optional[Dict]:
    cur   = df.iloc[-1]
    price = float(cur["Close"])

    sma20  = cur.get("SMA_20",  np.nan)
    sma50  = cur.get("SMA_50",  np.nan)
    sma200 = cur.get("SMA_200", np.nan)
    rsi    = cur.get("RSI",     np.nan)
    adx    = cur.get("ADX",     np.nan)
    atr    = cur.get("ATR",     np.nan)

    if any(pd.isna(v) for v in [sma20, sma50, sma200, rsi, atr]):
        return None
    if not (price > sma20 > sma50 > sma200):
        return None
    if not (rsi_lo < rsi < rsi_hi):
        return None
    if not pd.isna(adx) and adx < 20:
        return None
    if len(df) < perf_lookback + 2:
        return None

    perf_20d = float(df["Close"].pct_change(perf_lookback).iloc[-1]) * 100
    if perf_20d < min_perf_pct:
        return None

    sl = max(float(sma20) - 0.5 * atr, price - 2.0 * atr)
    return {
        "action":    "BUY",
        "screen":    "Momentum_Leader",
        "price":     round(price, 2),
        "sl":        round(sl, 2),
        "tp":        None,
        "rsi":       round(rsi, 2),
        "adx":       round(float(adx), 2) if not pd.isna(adx) else None,
        "perf_20d":  round(perf_20d, 2),
        "reason":    f"Price>SMA20>SMA50>SMA200 | RSI={rsi:.1f} | 20d={perf_20d:+.1f}%",
        "timestamp": datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Screen 3: Breakout
# ─────────────────────────────────────────────────────────────────────────────

def check_breakout(
    df:             pd.DataFrame,
    vol_multiplier: float = 1.5,
    pct_from_high:  float = 3.0,
) -> Optional[Dict]:
    cur   = df.iloc[-1]
    price = float(cur["Close"])
    rsi   = cur.get("RSI",          np.nan)
    v_rat = _session_volume_ratio(df)   # live session: projected / previous (volume_reading)
    adx   = cur.get("ADX",          np.nan)
    atr   = cur.get("ATR",          np.nan)

    if any(pd.isna(v) for v in [rsi, atr]):
        return None

    high_52w     = float(df["High"].tail(_52W_BARS).max())   # FIX SIG-52W
    pct_from_52h = (high_52w - price) / max(high_52w, 1) * 100

    if pct_from_52h > pct_from_high:
        return None
    if not pd.isna(v_rat) and v_rat < vol_multiplier:
        return None
    if rsi > 80:
        return None
    # FIX BRK-ADX: adx was fetched above but never checked — a breakout at
    # the 52-week high with no real trend strength behind it (low ADX) is a
    # much likelier fakeout than a confirmed move. check_momentum_leader()
    # already gates on this same threshold; mirror it here rather than
    # inventing a different cutoff for a sibling screen.
    if not pd.isna(adx) and adx < 20:
        return None

    sl = price - 2.0 * atr
    tp = price + 3.0 * atr
    return {
        "action":       "BUY",
        "screen":       "Breakout",
        "price":        round(price, 2),
        "sl":           round(sl, 2),
        "tp":           round(tp, 2),
        "rsi":          round(rsi, 2),
        # The "adx" key was listed twice here; the fix note at the top of this
        # module claimed the duplicate had been removed but it hadn't. Same
        # value both times, so it was harmless — just dead, and misleading to
        # anyone reading the note.
        "adx":          round(float(adx), 2) if not pd.isna(adx) else None,
        "vol_ratio":    round(float(v_rat), 2) if not pd.isna(v_rat) else None,
        "pct_from_52h": round(pct_from_52h, 2),
        # v_rat is allowed to be NaN here (the volume gate above is skipped
        # when it is), so format it defensively rather than printing "nan×".
        "reason":       (f"Near 52w high (−{pct_from_52h:.1f}%) | "
                         f"VolRatio={'n/a' if pd.isna(v_rat) else f'{float(v_rat):.2f}x'} | "
                         f"RSI={rsi:.1f}"
                         + ("" if pd.isna(adx) else f" | ADX={float(adx):.1f}")),
        "timestamp":    datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Screen 4: Pullback to SMA
# ─────────────────────────────────────────────────────────────────────────────

def check_pullback_to_sma(
    df:            pd.DataFrame,
    sma_target:    str   = "SMA_20",
    pct_tolerance: float = 2.0,
) -> Optional[Dict]:
    cur   = df.iloc[-1]
    price = float(cur["Close"])

    sma200 = cur.get("SMA_200",   np.nan)
    sma_t  = cur.get(sma_target,  np.nan)
    rsi    = cur.get("RSI",       np.nan)
    atr    = cur.get("ATR",       np.nan)

    if any(pd.isna(v) for v in [sma200, sma_t, rsi, atr]):
        return None
    if price <= sma200:
        return None
    if rsi >= 55:
        return None

    pct_from_sma = (price - float(sma_t)) / max(float(sma_t), 1) * 100
    if not (-pct_tolerance <= pct_from_sma <= pct_tolerance):
        return None

    bull_div = int(cur.get("RSI_Bull_Div",     0))
    bull_eng = int(cur.get("Pat_BullEngulfing", 0))
    hammer   = int(cur.get("Pat_Hammer",        0))

    extras = []
    if bull_div: extras.append("RSI_Div")
    if bull_eng: extras.append("BullEngulf")
    if hammer:   extras.append("Hammer")

    sl = price - 2.0 * atr
    tp = price + 3.0 * atr
    return {
        "action":       "BUY",
        "screen":       f"Pullback_{sma_target}",
        "price":        round(price, 2),
        "sl":           round(sl, 2),
        "tp":           round(tp, 2),
        "rsi":          round(rsi, 2),
        "pct_from_sma": round(pct_from_sma, 2),
        "reason":       f"Pullback to {sma_target} ({pct_from_sma:+.1f}%) | RSI={rsi:.1f}"
                        + (f" | {'+'.join(extras)}" if extras else ""),
        "timestamp":    datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Screen 5: Fibonacci Pullback
# ─────────────────────────────────────────────────────────────────────────────

def check_fibonacci_pullback(
    df:      pd.DataFrame,
    max_rsi: float = 60,
) -> Optional[Dict]:
    """
    FIX: gracefully returns None when Fib columns are absent instead of
    potentially erroring. Previously relied on columns always being present.
    """
    cur   = df.iloc[-1]
    price = float(cur["Close"])

    # If Fib columns aren't computed by add_all_indicators, return None gracefully
    fib_38 = cur.get("Fib_38_2",   np.nan)
    fib_62 = cur.get("Fib_61_8",   np.nan)
    near_38 = int(cur.get("Fib_Near_38", 0))
    near_62 = int(cur.get("Fib_Near_62", 0))

    # Exit early if Fib levels aren't available — don't attempt the screen
    if pd.isna(fib_38) or pd.isna(fib_62):
        return None
    if not (near_38 or near_62):
        return None

    sma200   = cur.get("SMA_200",  np.nan)
    rsi      = cur.get("RSI",      np.nan)
    atr      = cur.get("ATR",      np.nan)
    fib_78   = cur.get("Fib_78_6", np.nan)
    fib_high = cur.get("Fib_High", np.nan)

    if any(pd.isna(v) for v in [sma200, rsi, atr]):
        return None
    if price <= float(sma200):
        return None
    if rsi >= max_rsi:
        return None

    if near_38:
        level_name  = "38.2%"
        fib_support = float(fib_38)
        sl          = float(fib_62) * 0.99 if not pd.isna(fib_62) else price - 2.0 * atr
    else:
        level_name  = "61.8%"
        fib_support = float(fib_62)
        sl          = float(fib_78) * 0.99 if not pd.isna(fib_78) else price - 2.0 * atr

    tp = float(fib_high) if not pd.isna(fib_high) else price + 3.0 * atr
    rr = (tp - price) / max(price - sl, 1e-6)

    extras = []
    if int(cur.get("RSI_Bull_Div",     0)): extras.append("RSI_Div")
    if int(cur.get("Pat_Hammer",        0)): extras.append("Hammer")
    if int(cur.get("Pat_BullEngulfing", 0)): extras.append("BullEngulf")
    macd_h = cur.get("MACD_Hist", np.nan)
    if not pd.isna(macd_h) and float(macd_h) > 0:
        extras.append("MACD+")

    return {
        "action":      "BUY",
        "screen":      "Fib_Pullback",
        "price":       round(price, 2),
        "sl":          round(sl, 2),
        "tp":          round(tp, 2),
        "rsi":         round(rsi, 2),
        "fib_level":   level_name,
        "fib_support": round(fib_support, 2),
        "rr_ratio":    round(rr, 2),
        "reason":      f"Fib {level_name} pullback | RSI={rsi:.1f} | R:R={rr:.1f}x"
                       + (f" | {'+'.join(extras)}" if extras else ""),
        "timestamp":   datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Legacy signals (used by backtesting pipeline)
# ─────────────────────────────────────────────────────────────────────────────

def check_rsi_macd_signal(
    df:             pd.DataFrame,
    rsi_oversold:   float = 40,
    rsi_overbought: float = 55,
    atr_stop_mult:  float = 2.5,
    atr_tp_mult:    float = 3.2,
) -> Optional[Dict]:
    cur   = df.iloc[-1]
    prev  = df.iloc[-2]

    rsi    = cur.get("RSI",          np.nan)
    macd   = cur.get("MACD",         np.nan)
    sig    = cur.get("MACD_Signal",  np.nan)
    macd_p = prev.get("MACD",        np.nan)
    sig_p  = prev.get("MACD_Signal", np.nan)
    atr    = cur.get("ATR",          np.nan)
    price  = float(cur["Close"])

    if any(np.isnan(v) for v in [rsi, macd, sig, atr]):
        return None

    macd_cross_up   = (macd > sig)  and (macd_p <= sig_p)
    macd_cross_down = (macd < sig)  and (macd_p >= sig_p)

    if rsi < rsi_oversold and macd_cross_up:
        return {
            "action":    "BUY",
            "screen":    "RSI+MACD",
            "ticker":    None,
            "price":     round(price, 2),
            "sl":        round(price - atr_stop_mult * atr, 2),
            "tp":        round(price + atr_tp_mult   * atr, 2),
            "rsi":       round(rsi, 2),
            "strategy":  "RSI+MACD",
            "reason":    f"RSI={rsi:.1f} + MACD bullish cross",
            "timestamp": datetime.now().isoformat(),
        }
    if rsi > rsi_overbought or macd_cross_down:
        reason = "RSI overbought" if rsi > rsi_overbought else "MACD bearish crossover"
        return {
            "action":    "SELL",
            "screen":    "RSI+MACD",
            "ticker":    None,
            "price":     round(price, 2),
            "sl":        None, "tp": None,
            "rsi":       round(rsi, 2),
            "strategy":  "RSI+MACD",
            "reason":    reason,
            "timestamp": datetime.now().isoformat(),
        }
    return None


def check_momentum_signal(
    df:                 pd.DataFrame,
    momentum_threshold: float = 0.05,
    momentum_lookback:  int   = 20,
    sma_trend_period:   int   = 50,
    sma_exit_period:    int   = 20,
    atr_stop_mult:      float = 1.5,
) -> Optional[Dict]:
    cur   = df.iloc[-1]
    price = float(cur["Close"])
    close = df["Close"]

    if len(close) < max(momentum_lookback, sma_trend_period) + 5:
        return None

    momentum  = float(close.pct_change(momentum_lookback).iloc[-1])
    sma_trend = float(close.rolling(sma_trend_period).mean().iloc[-1])
    sma_exit  = float(close.rolling(sma_exit_period).mean().iloc[-1])
    atr       = cur.get("ATR", np.nan)

    if any(pd.isna(v) for v in [momentum, sma_trend, sma_exit]):
        return None

    if momentum > momentum_threshold and price > sma_trend:
        sl = price - atr_stop_mult * atr if not np.isnan(atr) else price * 0.97
        return {
            "action":    "BUY",
            "screen":    "Momentum",
            "ticker":    None,
            "price":     round(price, 2),
            "sl":        round(sl, 2),
            "tp":        None,
            "momentum":  round(momentum * 100, 2),
            "strategy":  "Momentum",
            "reason":    f"{momentum_lookback}d mom={momentum*100:+.2f}% | above SMA{sma_trend_period}",
            "timestamp": datetime.now().isoformat(),
        }
    if price < sma_exit or momentum < 0:
        reason = f"Below SMA{sma_exit_period}" if price < sma_exit else "Momentum turned negative"
        return {
            "action":   "SELL",
            "screen":   "Momentum",
            "ticker":   None,
            "price":    round(price, 2),
            "sl":       None, "tp": None,
            "momentum": round(momentum * 100, 2),
            "strategy": "Momentum",
            "reason":   reason,
            "timestamp": datetime.now().isoformat(),
        }
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Screen 6: VCP (Volatility Contraction Pattern — Minervini)
# ─────────────────────────────────────────────────────────────────────────────
#
# Mark Minervini's SEPA / VCP setup, adapted to daily bars with pandas-only
# math (no talib dependency). Not a signal our composite screens produce —
# it looks for the OPPOSITE profile of "oversold bounce": a Stage-2 uptrend
# in the middle of a quiet, contracting base right at the pivot.
#
# The 5-component score (0-100) matches Minervini's checklist:
#   * Trend       (25) — price > 200SMA, 50SMA > 200SMA, both rising
#   * Contraction (25) — recent 25-day range is < 65% of prior 25-day range
#   * Vol dry-up  (20) — last-20d avg volume < base 60-100d avg × 0.85
#   * Pivot near  (15) — within 12% of the 52-week high (pivot proximity)
#   * Relative    (15) — 3-month return > NIFTY 3-month return (RS positive)
#
# Anything below 60 fails the screen. NIFTY reference is read from the
# same fetcher cache the app already uses; missing NIFTY reduces the max
# score by 15 rather than failing the setup.
_VCP_MIN_SCORE = 60


def _nifty_3m_return() -> Optional[float]:
    """3-month % change of NIFTY 50 for the relative-strength component.
    Cached alongside every other fetch (5-min TTL). Returns None on error."""
    try:
        nf = fetch_single("^NSEI", period="6mo")
        if nf is None or len(nf) < 60:
            return None
        return float((nf["Close"].iloc[-1] / nf["Close"].iloc[-63] - 1.0) * 100)
    except Exception:
        return None


def check_vcp(df: pd.DataFrame) -> Optional[Dict]:
    """
    VCP (Minervini) — Stage-2 uptrend + tight base + volume dry-up + at pivot.
    Returns a signal dict on score ≥ _VCP_MIN_SCORE, None otherwise.

    Uses only columns add_all_indicators already produces (Close, High, Low,
    Volume, SMA_50, SMA_200, ATR). No new indicator required.
    """
    if df is None or len(df) < 220:      # need 200SMA + 20 bars of base
        return None

    cur   = df.iloc[-1]
    price = float(cur["Close"])
    sma50 = cur.get("SMA_50",  np.nan)
    sma200= cur.get("SMA_200", np.nan)
    atr   = cur.get("ATR",     np.nan)
    if any(pd.isna(v) for v in [sma50, sma200, atr]):
        return None

    score:    float          = 0.0
    reasons:  List[str]      = []
    details:  Dict[str, Any] = {}

    # 1) Trend (25) — Stage-2: above both MAs; 50 > 200; both sloping up
    sma50_slope_up  = float(df["SMA_50"].iloc[-1])  > float(df["SMA_50"].iloc[-20])
    sma200_slope_up = float(df["SMA_200"].iloc[-1]) > float(df["SMA_200"].iloc[-40])
    if price > sma50 > sma200 and sma50_slope_up and sma200_slope_up:
        score += 25
        reasons.append("Stage-2 uptrend")
        details["stage"] = "2"
    else:
        return None                       # Stage-2 is a hard prerequisite

    # 2) Contraction (25) — recent 25d range vs prior 25d range
    recent = df.iloc[-25:]
    prior  = df.iloc[-50:-25]
    recent_range = (recent["High"].max() - recent["Low"].min()) / price
    prior_range  = (prior["High"].max()  - prior["Low"].min())  / price
    if prior_range > 0:
        contraction = recent_range / prior_range
        details["contraction_ratio"] = round(float(contraction), 3)
        if contraction <= 0.55:
            score += 25; reasons.append(f"Tight base ({contraction*100:.0f}%)")
        elif contraction <= 0.70:
            score += 18; reasons.append(f"Contracting ({contraction*100:.0f}%)")
        elif contraction <= 0.85:
            score += 10

    # 3) Volume dry-up (20) — last-20d avg volume < base 60-100d avg × 0.85
    if "Volume" in df.columns:
        vol_recent = float(df["Volume"].iloc[-20:].mean())
        vol_base   = float(df["Volume"].iloc[-100:-20].mean())
        if vol_base > 0:
            ratio = vol_recent / vol_base
            details["vol_dryup_ratio"] = round(ratio, 3)
            if ratio <= 0.65:
                score += 20; reasons.append("Volume dry-up strong")
            elif ratio <= 0.85:
                score += 12; reasons.append("Volume dry-up")

    # 4) Pivot proximity (15) — within 12% of 52w high
    high_52w = float(df["High"].tail(_52W_BARS).max())
    pct_from = (high_52w - price) / max(high_52w, 1) * 100
    details["pct_from_52h"] = round(pct_from, 2)
    if pct_from <= 5:
        score += 15; reasons.append(f"At pivot (−{pct_from:.1f}%)")
    elif pct_from <= 12:
        score += 10; reasons.append(f"Near pivot (−{pct_from:.1f}%)")

    # 5) Relative strength vs NIFTY (15)
    try:
        r3m = float((price / float(df["Close"].iloc[-63]) - 1.0) * 100) \
              if len(df) >= 63 else None
    except Exception:
        r3m = None
    n3m = _nifty_3m_return()
    if r3m is not None:
        details["ret_3m_pct"]   = round(r3m, 2)
    if n3m is not None:
        details["nifty_3m_pct"] = round(n3m, 2)
    if r3m is not None and n3m is not None:
        if r3m - n3m >= 10:
            score += 15; reasons.append(f"RS strong (+{r3m-n3m:.1f}% vs NIFTY)")
        elif r3m > n3m:
            score += 8;  reasons.append(f"RS positive (+{r3m-n3m:.1f}% vs NIFTY)")
    elif r3m is not None and r3m > 0:
        # NIFTY unavailable — soft-credit an absolute-positive 3m return
        score += 5; reasons.append(f"3m return +{r3m:.1f}% (NIFTY n/a)")

    if score < _VCP_MIN_SCORE:
        return None

    # Pivot buy zone: high of the last 5 sessions (Minervini's typical entry)
    pivot = float(df["High"].tail(5).max())
    sl    = min(float(df["Low"].tail(20).min()), price - 2.0 * atr)
    tp    = price + 3.0 * (price - sl)
    return {
        "action":    "BUY",
        "screen":    "VCP",
        "price":     round(price, 2),
        "pivot":     round(pivot, 2),
        "sl":        round(sl, 2),
        "tp":        round(tp, 2),
        "vcp_score": round(score, 1),
        "reason":    f"VCP {score:.0f}/100: " + " · ".join(reasons),
        "details":   details,
        "timestamp": datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Multi-screen scan  (main entry point)
# ─────────────────────────────────────────────────────────────────────────────

_DASHBOARD_SINGLE_SCREEN_FNS = {
    # FIX SR1: explicit single-screen routing for the dashboard's screen-type
    # dropdown (06_smart_screener.py). Previously ANY strategy string other
    # than the two legacy CLI values ("rsi_macd", "momentum") fell through to
    # the "run all 5 screens, take first match" branch below — so selecting
    # "Oversold Bounce" silently ran Oversold+Fib+Pullback20+Pullback50+
    # Breakout+MomentumLeader combined instead of just Oversold.
    #
    # "momentum_leader" (NOT "momentum") is deliberately a distinct key from
    # the legacy "momentum" string, which main.py's CLI scan/backtest mode
    # still relies on to reach the OLD check_momentum_signal() for backward
    # compatibility — that routing is left untouched below.
    "oversold":        lambda d: check_oversold_bounce(d),
    "breakout":        lambda d: check_breakout(d),
    "pullback_SMA20":  lambda d: check_pullback_to_sma(d, "SMA_20"),
    "pullback_SMA50":  lambda d: check_pullback_to_sma(d, "SMA_50"),
    "fibonacci":       lambda d: check_fibonacci_pullback(d),
    "momentum_leader": lambda d: check_momentum_leader(d),
    "vcp":             lambda d: check_vcp(d),
}


def scan_tickers(
    tickers:       List[str],
    strategy:      str  = "all",
    period:        str  = "1y",
    params:        Optional[Dict] = None,
    use_vix:       bool = True,
    sector_filter: bool = False,
    top_n_sectors: int  = 3,
) -> List[Dict]:
    """
    Scan tickers across all 5 screens (or a single legacy strategy, or a
    single explicit dashboard screen — see _DASHBOARD_SINGLE_SCREEN_FNS).

    FIX: VIX block now uses `continue` instead of `break` so remaining
    screens are still tried when one screen's BUY is blocked by VIX.

    FIX SR1: strategy values in _DASHBOARD_SINGLE_SCREEN_FNS now run ONLY
    that one screen, instead of silently falling through to "run all 5".
    """
    params  = params or {}
    signals = []

    vix_info = {"allow_buy": True, "vix": None, "regime": "unknown"}
    if use_vix:
        try:
            vix_info = get_india_vix_regime()
        except Exception as e:
            _log.debug("scan_tickers: VIX regime lookup failed, proceeding without it: %s", e)

    vix_str = (f"  India VIX: {vix_info['vix']} | Regime: {vix_info['regime'].upper()}"
               if vix_info["vix"] else "  India VIX: unavailable")
    print(f"\n  Scanning {len(tickers)} tickers  |  strategy={strategy}")
    print(f"{vix_str}")
    if not vix_info["allow_buy"]:
        print(f"  VIX > 28 — BUY signals suppressed (panic regime)")
    print(f"  {'─'*56}")

    allowed_sectors: Optional[set] = None
    if sector_filter and strategy == "all":
        try:
            sec_scores = compute_sector_scores(period="1y")
            if not sec_scores.empty:
                allowed_sectors = set(sec_scores.head(top_n_sectors).index.tolist())
                print(f"  Sector filter ON — top {top_n_sectors}: {', '.join(sorted(allowed_sectors))}")
        except Exception as exc:
            print(f"  Sector filter error ({exc}) — filter disabled")

    if strategy == "rsi_macd":
        legacy_fn    = check_rsi_macd_signal
        use_legacy   = True
        single_fn    = None
    elif strategy == "momentum":
        legacy_fn    = check_momentum_signal
        use_legacy   = True
        single_fn    = None
    elif strategy in _DASHBOARD_SINGLE_SCREEN_FNS:
        # FIX SR1: run exactly the one requested screen instead of all 5.
        use_legacy   = False
        legacy_fn    = None
        single_fn    = _DASHBOARD_SINGLE_SCREEN_FNS[strategy]
    else:
        use_legacy   = False
        legacy_fn    = None
        single_fn    = None

    for ticker in tickers:
        try:
            if allowed_sectors is not None:
                ticker_sector = _TICKER_SECTOR.get(ticker)
                if ticker_sector and ticker_sector not in allowed_sectors:
                    print(f"  -- {ticker:<22}  skipped (sector={ticker_sector} not top {top_n_sectors})")
                    continue

            df  = fetch_single(ticker, period=period)
            df  = add_all_indicators(df)
            df.dropna(subset=["RSI", "MACD", "ATR"], inplace=True)
            if len(df) < 50:
                continue

            fired:       List[Dict] = []
            struct_stop = find_structure_stop(df, lookback=20)

            if use_legacy:
                sig = legacy_fn(df, **params)
                if sig:
                    if sig["action"] == "BUY" and not vix_info["allow_buy"]:
                        print(f"  -- {ticker:<22}  BUY suppressed (VIX panic)")
                        continue
                    sig["ticker"] = ticker
                    fired.append(sig)
            elif single_fn is not None:
                # FIX SR1: exactly one screen requested — don't fall through
                # to the other 4/5 screens if it doesn't fire.
                sig = single_fn(df)
                if sig:
                    if sig["action"] == "BUY" and not vix_info["allow_buy"]:
                        print(f"  -- {ticker:<22}  BUY suppressed (VIX panic)")
                    else:
                        sig["ticker"]   = ticker
                        sig["strategy"] = sig["screen"]
                        sig["sector"]   = _TICKER_SECTOR.get(ticker, "Unknown")

                        if struct_stop and struct_stop > 0 and sig["action"] in ("BUY", "WATCHLIST"):
                            atr_sl = sig.get("sl", 0) or 0
                            if struct_stop > atr_sl:
                                sig["sl"]        = struct_stop
                                sig["stop_type"] = "structure"
                            else:
                                sig["stop_type"] = "atr"
                            sig["structure_stop"] = struct_stop

                        fired.append(sig)
            else:
                # FIX SR2: check every screen (not "stop at the first match")
                # so a later, stronger screen can't be silently pre-empted by
                # an earlier, weaker one — see fix note above for the
                # concrete Oversold-WATCHLIST vs Pullback-BUY scenario this
                # was masking.
                _ACTION_STRENGTH = {"BUY": 2, "WATCHLIST": 1, "SELL": 1, "HOLD": 0}
                candidates = []
                for fn in [
                    lambda d: check_oversold_bounce(d),
                    lambda d: check_fibonacci_pullback(d),
                    lambda d: check_pullback_to_sma(d, "SMA_20"),
                    lambda d: check_pullback_to_sma(d, "SMA_50"),
                    lambda d: check_breakout(d),
                    lambda d: check_momentum_leader(d),
                ]:
                    sig = fn(df)
                    if not sig:
                        continue   # screen didn't fire — try next

                    if sig["action"] == "BUY" and not vix_info["allow_buy"]:
                        # BUY blocked by VIX panic regime — try other screens
                        # (a later screen might still produce a WATCHLIST)
                        continue

                    candidates.append(sig)

                if candidates:
                    # Strongest action wins; original screen-check order is
                    # the tie-break among equal-strength matches (stable sort).
                    sig = max(candidates, key=lambda s: _ACTION_STRENGTH.get(s["action"], 0))

                    sig["ticker"]   = ticker
                    sig["strategy"] = sig["screen"]
                    sig["sector"]   = _TICKER_SECTOR.get(ticker, "Unknown")

                    if struct_stop and struct_stop > 0 and sig["action"] in ("BUY", "WATCHLIST"):
                        atr_sl = sig.get("sl", 0) or 0
                        if struct_stop > atr_sl:
                            sig["sl"]        = struct_stop
                            sig["stop_type"] = "structure"
                        else:
                            sig["stop_type"] = "atr"
                        sig["structure_stop"] = struct_stop

                    fired.append(sig)   # only one signal per ticker

            if fired:
                for sig in fired:
                    signals.append(sig)
                    icon = "🟢" if sig["action"] == "BUY" else (
                           "👁️" if sig["action"] == "WATCHLIST" else "🔴")
                    screen_tag = sig.get("screen", sig.get("strategy", ""))
                    print(f"  {icon} {ticker:<22}  [{screen_tag:<20}]  "
                          f"{sig['action']}  Rs.{sig['price']:,.2f}  — {sig['reason']}")
            else:
                print(f"  ⚪ {ticker:<22}  no signal")

        except Exception as e:
            print(f"  ⚠️  {ticker:<22}  error: {e}")

    print(f"\n  {len(signals)} signal(s) fired out of {len(tickers)} tickers scanned.")
    return signals
