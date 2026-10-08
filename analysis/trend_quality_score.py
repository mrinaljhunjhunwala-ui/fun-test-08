"""
trend_quality_score.py
======================
Trend Quality Score (TQS) — max 90 points across 4 equally-weighted pillars.

Refactored to calculate all indicators natively using standard pandas/numpy
to resolve dependency installation failures (e.g., pandas-ta packaging issues).
"""

from __future__ import annotations
import warnings
# FIX WARN1 — narrowed from a blanket `filterwarnings("ignore")` so numpy's
# RuntimeWarnings (invalid value / divide by zero / all-NaN slice) stay visible.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)

import os
import sys

# ── Ensure project root is on sys.path so data.fetcher resolves correctly ────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np
import pandas as pd
import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple, Union

pd.options.mode.chained_assignment = None

_log = logging.getLogger("analysis.trend_quality_score")


# ─────────────────────────────────────────────────────────────────────────────
# Data fetching
# ─────────────────────────────────────────────────────────────────────────────

def fetch_data(ticker: str, period: str = "5y") -> pd.DataFrame:
    """
    Use the repo's tiered data fetcher. Falls back to yfinance if unavailable.
    """
    try:
        from data.fetcher import fetch_single
        df = fetch_single(ticker, period=period)
    except Exception as e:
        _log.debug("%s: tiered fetcher unavailable, falling back to raw yfinance: %s", ticker, e)
        import yfinance as yf
        # Raw basis, same as data.fetcher (FIX OHLC-BASIS).
        df = yf.download(ticker, period=period, auto_adjust=False, progress=False)
        # yfinance 1.x: MultiIndex (Price, Ticker) columns even for one ticker.
        if isinstance(df.columns, pd.MultiIndex):
            lvl = next((i for i in range(df.columns.nlevels)
                        if "Close" in set(df.columns.get_level_values(i))), 0)
            df.columns = df.columns.get_level_values(lvl)
        if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
            df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)

    if df is None or df.empty:
        raise ValueError(f"{ticker}: no data returned")
    
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        df = df.rename(columns={c.lower(): c for c in required_cols})
        
    return df[required_cols].dropna()


# ─────────────────────────────────────────────────────────────────────────────
# Native Indicator Calculations (Replaces pandas-ta)
# ─────────────────────────────────────────────────────────────────────────────

def _fast_slope(series: pd.Series, window: int) -> pd.Series:
    """Vectorized rolling OLS slope."""
    x = np.arange(window, dtype=float)
    x -= x.mean()
    denom = (x ** 2).sum()
    if denom == 0:
        return pd.Series(np.nan, index=series.index)
    return series.rolling(window).apply(
        lambda y: float(np.dot(x, y - y.mean()) / denom), raw=True
    )


def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    Welles Wilder's RSI.

    FIX TQS-RSI — this ended with a blanket `rsi.fillna(50)`, which is the
    exact bug utils/indicators.py documents as FIX IND1, left unfixed in this
    parallel implementation.

    When a stock has zero down-days across the smoothing window — a real,
    routine state for an NSE momentum name in a strong multi-day rally, not a
    corner case — `avg_loss` is 0, `replace(0, nan)` makes RS NaN, and RSI comes
    out NaN. The conventional value there is 100 (no losses at all = maximally
    overbought). Filling it with 50 instead did not merely lose information, it
    inverted the reading: _score_all_pillars() awards its P3 RSI pillar
        RSI 45–55  →  9.00 pts   (the "healthy neutral" bucket)
        RSI > 80   →  2.25 pts   (the "running too hot" bucket)
    so the strongest-momentum stocks in the universe were scored as calm and
    neutral and handed 4× the points the rule intends — a silent, systematic
    ranking error in exactly the names a trend-quality score exists to rank.
    The same fillna also painted the leading warm-up bars (before `period` bars
    of history exist) as a confident neutral 50 rather than "not yet known";
    those bars are now left NaN and dropped by add_indicators()'s dropna, the
    same way SMA200/ADX warm-up already is.

    min_periods=period matches utils/indicators.add_rsi(), so the two RSI
    implementations in this repo now agree bar for bar.
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Use standard Wilder's smoothing alpha = 1 / period
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    no_loss = avg_loss == 0
    rsi = rsi.mask(no_loss & (avg_gain > 0), 100.0)   # zero down-days → 100
    rsi = rsi.mask(no_loss & (avg_gain == 0), 50.0)   # dead flat → neutral 50
    return rsi


def _compute_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Standard MACD lines."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    macd_hist = macd_line - signal_line
    return macd_line, signal_line, macd_hist


def _compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Average Directional Index (ADX)."""
    h_l = high - low
    h_pc = (high - close.shift(1)).abs()
    l_pc = (low - close.shift(1)).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    
    up_move = high.diff()
    down_move = low.shift(1) - low
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    # Wilder's smoothing representation
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    smoothed_plus_dm = pd.Series(plus_dm, index=high.index).ewm(alpha=1/period, adjust=False).mean()
    smoothed_minus_dm = pd.Series(minus_dm, index=high.index).ewm(alpha=1/period, adjust=False).mean()
    
    plus_di = 100 * (smoothed_plus_dm / atr.replace(0, np.nan))
    minus_di = 100 * (smoothed_minus_dm / atr.replace(0, np.nan))
    
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx


_VOL_RATIO_CAP = 3.0


def _up_down_volume_ratio(up_vol: pd.Series, down_vol: pd.Series,
                          window: int = 20) -> pd.Series:
    """20-bar up-volume / down-volume ratio, capped at _VOL_RATIO_CAP.

    FIX TQS-VOLRATIO (2026-09-24) — zero down-volume used to divide by NaN and
    get filled with 1.0 (-> 1.125 pts), so a window with ONLY up-days scored
    like a neutral one. Same shape as the earlier RSI zero-loss fix: positive
    up-volume with zero down-volume is the strongest possible reading and
    maps to the cap; zero up AND zero down (no movement) stays neutral 1.0.
    """
    up = up_vol.rolling(window, min_periods=5).sum()
    dn = down_vol.rolling(window, min_periods=5).sum()
    ratio = up / dn.replace(0, np.nan)
    ratio = ratio.mask((dn == 0) & (up > 0), _VOL_RATIO_CAP)
    return ratio.fillna(1.0).clip(upper=_VOL_RATIO_CAP)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # ── P1: Trend Strength ────────────────────────────────────────────────────
    df["SMA20"]  = df["Close"].rolling(20).mean()
    df["SMA50"]  = df["Close"].rolling(50).mean()
    df["SMA200"] = df["Close"].rolling(200).mean()
    df["SMA200_Slope"] = (df["SMA200"].pct_change(5) / 5) * 100

    df["ADX"] = _compute_adx(df["High"], df["Low"], df["Close"], period=14)

    # ── P2: Trend Persistence (rolling Sharpe) ────────────────────────────────
    ret = df["Close"].pct_change()

    def rolling_sharpe(returns: pd.Series, window: int) -> pd.Series:
        mu    = returns.rolling(window, min_periods=max(5, window // 4)).mean()
        sigma = returns.rolling(window, min_periods=max(5, window // 4)).std(ddof=1).replace(0, np.nan)
        return (mu / sigma * np.sqrt(252)).clip(-3.0, 3.0)

    df["Sharpe_5"]  = rolling_sharpe(ret, 5)
    df["Sharpe_20"] = rolling_sharpe(ret, 20)
    df["Sharpe_60"] = rolling_sharpe(ret, 60)

    # ── P3: Momentum Quality ──────────────────────────────────────────────────
    df["RSI"] = _compute_rsi(df["Close"], period=14)

    macd_line, signal_line, macd_hist = _compute_macd(df["Close"])
    df["MACD"] = macd_line
    df["MACD_Sig"] = signal_line
    df["MACD_Hist"] = macd_hist
    df["MACD_Hist_Delta"] = df["MACD_Hist"].diff()

    # ── P4: Technical Confirmation ────────────────────────────────────────────
    # Native OBV calculation: cumulative sum of (volume * sign of change)
    close_diff = df["Close"].diff()
    direction = np.sign(close_diff).fillna(0)
    df["OBV"] = (direction * df["Volume"]).cumsum()

    obv_mu    = df["OBV"].rolling(20, min_periods=5).mean()
    obv_sigma = df["OBV"].rolling(20, min_periods=5).std(ddof=1).replace(0, np.nan)
    df["OBV_Z"] = ((df["OBV"] - obv_mu) / obv_sigma).clip(-3.0, 3.0)

    df["OBV_Slope_20"]      = _fast_slope(df["OBV"], 20)
    df["OBV_Slope_PctRank"] = df["OBV_Slope_20"].rolling(252, min_periods=60).rank(pct=True)

    up_vol   = ret.gt(0) * df["Volume"]
    down_vol = ret.lt(0) * df["Volume"]
    df["Vol_Ratio"] = _up_down_volume_ratio(up_vol, down_vol)

    # Clean missing variables before parsing TQS
    df.dropna(subset=["SMA200", "ADX", "RSI", "Sharpe_20"], inplace=True)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Vectorized pillar scoring
# ─────────────────────────────────────────────────────────────────────────────

def _score_all_pillars(df: pd.DataFrame) -> pd.DataFrame:
    c = df

    # ── P1: Trend Strength (max 22.5) ─────────────────────────────────────────
    p1_align = np.where(
        (c["Close"] > c["SMA20"]) & (c["SMA20"] > c["SMA50"]) & (c["SMA50"] > c["SMA200"]), 12.0,
        np.where((c["Close"] > c["SMA50"]) & (c["SMA50"] > c["SMA200"]), 8.0,
        np.where(c["Close"] > c["SMA200"], 4.0, 0.0)))

    p1_adx = np.where(c["ADX"] > 40, 6.0,
             np.where(c["ADX"] > 30, 4.5,
             np.where(c["ADX"] > 25, 3.0,
             np.where(c["ADX"] >= 20, 1.5, 0.0))))

    p1_slope = np.where(c["SMA200_Slope"] > 0.05, 4.5,
               np.where(c["SMA200_Slope"] > 0.01, 2.5,
               np.where(c["SMA200_Slope"] >= 0.0, 1.0, 0.0)))

    p1 = p1_align + p1_adx + p1_slope

    # ── P2: Trend Persistence (max 22.5) ──────────────────────────────────────
    p2_s5 = np.where(c["Sharpe_5"] > 2.0, 5.0,
            np.where(c["Sharpe_5"] >= 1.0, 3.75,
            np.where(c["Sharpe_5"] >= 0.0, 2.5,
            np.where(c["Sharpe_5"] >= -1.0, 1.25, 0.0))))

    p2_s20 = np.where(c["Sharpe_20"] > 2.0, 10.0,
             np.where(c["Sharpe_20"] >= 1.5, 8.0,
             np.where(c["Sharpe_20"] >= 1.0, 6.0,
             np.where(c["Sharpe_20"] >= 0.0, 3.0,
             np.where(c["Sharpe_20"] >= -1.0, 1.0, 0.0)))))

    p2_s60 = np.where(c["Sharpe_60"] > 2.0, 7.5,
             np.where(c["Sharpe_60"] >= 1.5, 6.0,
             np.where(c["Sharpe_60"] >= 1.0, 4.5,
             np.where(c["Sharpe_60"] >= 0.0, 2.25,
             np.where(c["Sharpe_60"] >= -1.0, 0.75, 0.0)))))

    p2 = p2_s5 + p2_s20 + p2_s60

    # ── P3: Momentum Quality (max 22.5) ───────────────────────────────────────
    p3_rsi = np.where((c["RSI"] >= 55) & (c["RSI"] <= 70), 13.5,
             np.where((c["RSI"] >= 45) & (c["RSI"] < 55), 9.0,
             np.where((c["RSI"] > 70) & (c["RSI"] <= 80), 6.75,
             np.where((c["RSI"] >= 30) & (c["RSI"] < 45), 4.5,
             np.where(c["RSI"] > 80, 2.25, 0.0)))))

    is_bull     = c["MACD"] > c["MACD_Sig"]
    hist_pos    = c["MACD_Hist"] > 0
    hist_rising = c["MACD_Hist_Delta"] > 0
    hist_std5   = c["MACD_Hist"].rolling(5, min_periods=1).std().fillna(0)
    near_cross  = (c["MACD_Hist"].abs() < hist_std5) & (hist_std5 > 0)

    p3_macd = np.where(is_bull & hist_pos & hist_rising, 9.0,
              np.where(is_bull & hist_pos, 6.75,
              np.where(is_bull, 4.5,
              np.where(near_cross, 2.25, 0.0))))

    p3 = p3_rsi + p3_macd

    # ── P4: Technical Confirmation (max 22.5) ─────────────────────────────────
    p4_z = np.where(c["OBV_Z"] >= 2.0, 13.5,
           np.where(c["OBV_Z"] >= 1.0, 10.125,
           np.where(c["OBV_Z"] >= 0.0, 6.75,
           np.where(c["OBV_Z"] >= -1.0, 3.375, 0.0))))

    rank_col = c["OBV_Slope_PctRank"].fillna(0.5)
    p4_slope = np.where(rank_col >= 0.75, 5.625,
               np.where(rank_col >= 0.50, 3.75,
               np.where(rank_col >= 0.25, 1.875, 0.0)))

    p4_ratio = np.where(c["Vol_Ratio"] > 1.5, 3.375,
               np.where(c["Vol_Ratio"] > 1.2, 2.25,
               np.where(c["Vol_Ratio"] >= 1.0, 1.125, 0.0)))

    p4 = p4_z + p4_slope + p4_ratio

    out = df[["Close"]].copy()
    out["P1_Strength"]     = p1.round(3)
    out["P2_Persistence"]  = p2.round(3)
    out["P3_Momentum"]     = p3.round(3)
    out["P4_Confirmation"] = p4.round(3)
    out["TQS"]             = (p1 + p2 + p3 + p4).round(2)
    out["RSI"]             = df["RSI"].round(2)
    out["ADX"]             = df["ADX"].round(2)
    out["Sharpe_20"]       = df["Sharpe_20"].round(3)
    out["OBV_Z"]           = df["OBV_Z"].round(3)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TQSResult:
    ticker:     str
    date:       str
    close:      float
    tqs:        float
    p1:         float
    p2:         float
    p3:         float
    p4:         float
    rsi:        float
    adx:        float
    sharpe_20:  float
    obv_z:      float

    def grade(self) -> str:
        for thresh, g in [(80,"A+"), (70,"A"), (55,"B"), (40,"C"), (25,"D")]:
            if self.tqs >= thresh: return g
        return "F"

    def signal(self) -> str:
        if self.tqs >= 75: return "STRONG TREND"
        if self.tqs >= 60: return "TRENDING"
        if self.tqs >= 45: return "NEUTRAL"
        if self.tqs >= 30: return "WEAK"
        return "AVOID"

    def as_dict(self) -> Dict:
        return {
            "ticker": self.ticker, "date": self.date, "close": self.close,
            "tqs": self.tqs, "grade": self.grade(), "signal": self.signal(),
            "p1_strength": self.p1, "p2_persistence": self.p2,
            "p3_momentum": self.p3, "p4_confirmation": self.p4,
            "rsi": self.rsi, "adx": self.adx,
            "sharpe_20": self.sharpe_20, "obv_z": self.obv_z,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def score_ticker(
    ticker: str,
    period: str = "2y",
    last_n: int = 1,
) -> Union[TQSResult, List[TQSResult]]:
    """Score one ticker using native logic. Warmup windows are padded automatically.

    FIX TQS-PERIOD — the padding table mapped "5y" → "6y", but "6y" is not a
    period either data source understands. data/fetcher.py's `_PERIOD_DAYS` has
    no "6y" key, so `_period_to_dates()` fell through to its 370-day default for
    Stooq; `_fetch_yahoo_direct`'s `_RANGE_MAP` has no "6y" key either, so it
    fell through to its "1y" default. Net effect: asking for a 5-year TQS
    silently returned ONE year of history — no error, no warning, just a
    quietly truncated series, with SMA200 / the 252-bar OBV slope percentile /
    the 60-bar Sharpe all computed off a fraction of the intended window.
    "max" (1,830 days ≈ 5 years) is the longest span both sources actually
    support, so it is the honest ceiling here; padding beyond 5 years is not
    available from these providers.
    """
    warm_periods = {"1y": "2y", "2y": "3y", "5y": "max"}
    padded_period = warm_periods.get(period, period)

    df_raw = fetch_data(ticker, period=padded_period)
    df_ind = add_indicators(df_raw)
    df_tqs = _score_all_pillars(df_ind)

    results = []
    # The previous `.tail(max(last_n, session_limit)).tail(last_n)` was just
    # `.tail(last_n)` — the outer window was always ≥ the inner one, so the
    # first slice never removed a row the second wouldn't have.
    for idx, row in df_tqs.tail(last_n).iterrows():
        results.append(TQSResult(
            ticker=ticker, date=str(idx.date()) if hasattr(idx, "date") else str(idx),
            close=float(row["Close"]), tqs=float(row["TQS"]),
            p1=float(row["P1_Strength"]), p2=float(row["P2_Persistence"]),
            p3=float(row["P3_Momentum"]), p4=float(row["P4_Confirmation"]),
            rsi=float(row["RSI"]), adx=float(row["ADX"]),
            sharpe_20=float(row["Sharpe_20"]), obv_z=float(row["OBV_Z"]),
        ))
        
    if not results:
        raise ValueError(f"Insufficient active history returned for {ticker}.")
        
    return results[0] if last_n == 1 else results


def scan_universe(tickers: List[str], period: str = "1y") -> pd.DataFrame:
    """Score all tickers, return DataFrame sorted by TQS descending."""
    rows = []
    for t in tickers:
        try:
            r = score_ticker(t, period=period)
            if isinstance(r, list):
                r = r[-1]
            rows.append(r.as_dict())
            print(f"  ✓ {t:20s}  TQS={r.tqs:5.1f}  [{r.grade()}]  {r.signal()}")
        except Exception as e:
            print(f"  ✗ {t:20s}  skipped: {e}")
            
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("tqs", ascending=False).reset_index(drop=True)


def print_report(r: TQSResult) -> None:
    bar = lambda v, mx: "█" * int(v / mx * 20) + "░" * (20 - int(v / mx * 20))
    print(f"\n{'━'*56}")
    print(f"  TQS Report — {r.ticker}  [{r.date}]")
    print(f"{'━'*56}")
    print(f"  Close:    ₹{r.close:>10,.2f}")
    print(f"  Grade:    {r.grade():<4}  Signal: {r.signal()}")
    print(f"\n  TQS Total   {r.tqs:5.1f}/90   {bar(r.tqs, 90)}")
    print(f"  P1 Strength {r.p1:5.1f}/22.5 {bar(r.p1, 22.5)}")
    print(f"  P2 Persist  {r.p2:5.1f}/22.5 {bar(r.p2, 22.5)}")
    print(f"  P3 Momentum {r.p3:5.1f}/22.5 {bar(r.p3, 22.5)}")
    print(f"  P4 Volume   {r.p4:5.1f}/22.5 {bar(r.p4, 22.5)}")
    print(f"\n  RSI:      {r.rsi:5.1f}    ADX:      {r.adx:5.1f}")
    print(f"  Sharpe20: {r.sharpe_20:5.2f}    OBV Z:    {r.obv_z:5.2f}")
    print(f"{'━'*56}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    TICKER = "RELIANCE.NS"
    print(f"Scoring {TICKER} with warmup padding...")
    
    try:
        result = score_ticker(TICKER, period="2y")
        print_report(result)

        history = score_ticker(TICKER, period="2y", last_n=5)
        print("Last 5 sessions:")
        if isinstance(history, list):
            for h in history:
                print(f"  {h.date}  TQS={h.tqs:5.1f}  P1={h.p1:.1f} "
                      f"P2={h.p2:.1f} P3={h.p3:.1f} P4={h.p4:.1f}  {h.signal()}")
        else:
            print(f"  {history.date}  TQS={history.tqs:5.1f}  {history.signal()}")

        NIFTY_SAMPLE = [
            "RELIANCE.NS", "TCS.NS",       "HDFCBANK.NS",  "INFY.NS",
            "ICICIBANK.NS","HINDUNILVR.NS", "ITC.NS",       "SBIN.NS",
            "BHARTIARTL.NS","KOTAKBANK.NS", "AXISBANK.NS",  "WIPRO.NS",
        ]
        print(f"\nScanning {len(NIFTY_SAMPLE)} stocks...\n")
        scan_df = scan_universe(NIFTY_SAMPLE, period="1y")
        if not scan_df.empty:
            print("\n" + "─" * 90)
            print(scan_df[[
                "ticker","close","tqs","grade","signal",
                "p1_strength","p2_persistence","p3_momentum","p4_confirmation",
                "rsi","sharpe_20","obv_z"
            ]].to_string(index=False))
    except Exception as err:
        print(f"Calculation failed: {err}")
