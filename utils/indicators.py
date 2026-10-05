"""
utils/indicators.py
Computes technical indicators: MA, RSI, MACD, BB, ATR, VWAP, ADX, Stochastic,
Fibonacci retracements, Supertrend, CPR/Pivot Points,
candlestick patterns, and RSI divergence.
Uses pure pandas/numpy — no TA-Lib C library required.

FIX IND1 — add_rsi(): when a stock has zero down-days inside the lookback
window (a realistic case during a strong multi-day rally — common for NSE
momentum names), avg_loss hits exactly 0, so the old
`avg_gain / avg_loss.replace(0, np.nan)` produced RS = NaN and therefore
RSI = NaN for the entire stretch, instead of the conventional RSI = 100
(no losses at all = maximally overbought). Verified with a synthetic
20-day pure-uptrend series: RSI silently went NaN once avg_loss hit 0, not
100. Since RSI feeds composite/momentum scoring, this meant exactly the
strongest-momentum candidates could drop out of ranked/sorted output or
get miscompared against NaN. Fixed by explicitly setting RSI = 100 where
avg_loss == 0 and avg_gain > 0 (and RSI = 50 in the degenerate all-flat
case where both are 0, rather than leaving it NaN).

FIX IND2 — add_supertrend(): three independent bugs, all verified against
synthetic OHLC data with controlled ATR.

  (c) NaN-poisoning bug — THE dominant one, affecting every single call in
      production: `upper_band = upper_raw.values.copy()` seeds the whole
      band array directly from the raw ATR-based formula, whose row 0 is
      ALWAYS NaN for any real OHLCV series (ATR's rolling window needs
      `period` rows before its first valid value — true for any period,
      not a corner case). The tightening loop —
          if upper_raw.iloc[i] < upper_band[i-1] or close[i-1] > upper_band[i-1]:
              upper_band[i] = upper_raw.iloc[i]
          else:
              upper_band[i] = upper_band[i-1]
      — can never recover from a NaN previous value: any comparison
      against NaN evaluates False (IEEE754), so neither branch condition
      can ever be True once upper_band[i-1] is NaN, and "else" just
      re-copies the same NaN forward forever — even long after upper_raw
      itself becomes perfectly valid. Verified directly: fed a synthetic
      series with ATR NaN for the first 9 bars then valid (3.0) from bar 9
      onward, and ST_Upper stayed NaN for all 20 bars regardless. In
      production this means Supertrend / ST_Upper / ST_Lower /
      ST_Direction / ST_Signal were ALL NaN for the ENTIRE history of
      EVERY stock, on every page that calls add_supertrend() — not a rare
      edge case, the default behavior. Fixed by finding the first index
      where the raw bands are actually valid and seeding the recursion
      there (mirroring what the old code intended to do at index 0),
      rather than seeding from a row that's structurally guaranteed NaN.

  (a) Initialization-order bug: even setting aside (c), `supertrend[0] =
      upper_band[0]` and `direction[0] = -1` were set AFTER the main
      `for i in range(1, n)` loop, but the loop's very first iteration
      (i=1) reads `prev_st = supertrend[i - 1]` i.e. `supertrend[0]` —
      which at that point is still its `np.zeros(n)` default (0.0), not
      the intended seed value. The `prev_st = supertrend[i-1] if i > 0
      else upper_band[0]` ternary's else-branch is dead code, since i is
      always > 0 in this loop. Net effect, confirmed on a plain 6-bar
      monotonic downtrend (100→98→96→94→92→90 — nothing ambiguous about
      this trend): the indicator flashed BULLISH (+1) for 4 consecutive
      bars right at the start before self-correcting back to bearish at
      bar 5. Since ST_Signal (buy/sell flags) is derived from direction
      changes, this is a false BUY signal fired into an obvious
      downtrend. Fixed (now folded into the (c) fix) by seeding at the
      first valid bar BEFORE the loop runs, instead of after.

  (b) Band-comparison bug: the trend-flip check compared the current
      bar's close against the CURRENT bar's band (`upper_band[i]` /
      `lower_band[i]`), which may have already been tightened earlier in
      the same iteration. The canonical Supertrend definition (matching
      TradingView's built-in indicator — the reference Indian traders
      actually look at) compares against the PRIOR bar's band
      (`upper_band[i-1]` / `lower_band[i-1]`), since that's the level
      that was actually in force when the bar closed. Verified with a
      volatility-squeeze synthetic case (ATR contracting sharply mid-
      series): even after fixing (a), the old current-bar comparison
      still flipped bullish one bar earlier than the prior-bar comparison
      — i.e. a real, independent discrepancy from the standard
      definition, not just a side-effect of (a).

  ST_Direction is now 0 (not a fake +1) for the unavoidable warm-up bars
  before ATR has enough data — distinguishable from real -1/+1 readings —
  and ST_Signal is explicitly suppressed on the first real bar (no prior
  trend exists yet to "flip" from).
"""

import logging
import pandas as pd
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from typing import Optional, Iterable, List

# FIX IND-LOG — add_avwap() and add_relative_strength() both call
# _log.debug(...) in their except blocks (graceful NaN-column fallback on
# calculation failure), but this module never defined _log. That meant any
# real failure inside those try blocks didn't degrade gracefully as
# intended — it raised NameError: name '_log' is not defined instead,
# masking the original exception and turning a soft, recoverable failure
# into a hard crash for the caller at exactly the moment robustness matters.
_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Core call: add everything to a DataFrame
# ─────────────────────────────────────────────────────────────────────────────

# FIX LAZY1 — add_all_indicators() unconditionally computed all 14 indicator
# groups on every call, even for callers (analysis/score.py's screening path)
# that only read a handful of the resulting columns. For a 500-ticker screen
# this meant computing Bollinger Bands, VWAP, Stochastic, Fibonacci levels,
# Supertrend, pivot points, and candlestick patterns on every stock, purely
# to throw those columns away unused.
#
# `groups=None` (the default) computes every group exactly as before — this
# is a strict backward-compat guarantee for the 14+ existing call sites
# (backtest/*, trading/*, models/*, dashboard/*, tests/*) that rely on the
# full column set. Only a caller that has verified which columns it actually
# reads (like score_stock(), see analysis/score.py) should pass an explicit
# subset.
#
# _INDICATOR_GROUPS itself is populated at the BOTTOM of this file (after
# every add_*/detect_* function has been defined — Python can't reference a
# function before it exists). add_all_indicators() below only reads that
# dict at call time, well after module import has finished populating it,
# so the forward reference is safe.

# Groups whose output is only meaningful if a dependency group also ran.
# detect_rsi_divergence() degrades gracefully (returns 0/0 columns) rather
# than crashing if RSI is missing, but that's a silent-wrong-answer trap —
# so if "divergence" is explicitly requested, pull "rsi" in automatically
# rather than let it silently produce all-zero divergence flags.
_GROUP_DEPENDENCIES = {"divergence": ("rsi",)}

_ALL_GROUPS = (
    "ma", "rsi", "macd", "bollinger", "atr", "vwap", "adx", "stochastic",
    "volume", "returns", "fibonacci", "supertrend", "pivot", "patterns",
    "divergence",
)


def add_all_indicators(df: pd.DataFrame, groups: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """
    Add technical indicators to a single-stock OHLCV DataFrame.

    Args:
        df:     OHLCV DataFrame (must have Open/High/Low/Close/Volume).
        groups: Optional subset of indicator groups to compute. Valid names:
                "ma", "rsi", "macd", "bollinger", "atr", "vwap", "adx",
                "stochastic", "volume", "returns", "fibonacci", "supertrend",
                "pivot", "patterns", "divergence".
                When None (default), ALL groups are computed — identical to
                the original unconditional behavior, so every existing
                caller is unaffected. Pass an explicit subset only when
                you've verified which columns your code actually reads;
                requesting too narrow a subset will raise KeyError deep in
                unrelated code the moment it tries to read a column you
                didn't ask for.

    Returns:
        df with the requested indicator columns added.

    Raises:
        ValueError: if `groups` contains an unrecognized group name.
    """
    if groups is None:
        selected: List[str] = list(_ALL_GROUPS)
    else:
        selected = list(dict.fromkeys(groups))  # de-dupe, preserve order
        unknown = set(selected) - set(_ALL_GROUPS)
        if unknown:
            raise ValueError(
                f"Unknown indicator group(s): {sorted(unknown)}. "
                f"Valid groups: {list(_ALL_GROUPS)}"
            )
        # Auto-include hard dependencies not explicitly requested (see
        # _GROUP_DEPENDENCIES docstring above) so a partial subset never
        # silently produces trivially-wrong output.
        for grp in list(selected):
            for dep in _GROUP_DEPENDENCIES.get(grp, ()):
                if dep not in selected:
                    selected.insert(selected.index(grp), dep)

    for grp in selected:
        df = _INDICATOR_GROUPS[grp](df)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Moving Averages
# ─────────────────────────────────────────────────────────────────────────────

def add_moving_averages(df: pd.DataFrame) -> pd.DataFrame:
    """Simple and Exponential Moving Averages."""
    for period in [5, 10, 20, 50, 200]:
        df[f"SMA_{period}"] = df["Close"].rolling(window=period).mean()
        df[f"EMA_{period}"] = df["Close"].ewm(span=period, adjust=False).mean()
    return df


# ─────────────────────────────────────────────────────────────────────────────
# RSI
# ─────────────────────────────────────────────────────────────────────────────

def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Relative Strength Index (Wilder's smoothing)."""
    delta    = df["Close"].diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    rsi      = 100 - (100 / (1 + rs))

    # FIX IND1: avg_loss == 0 (zero down-days in the window — a real
    # multi-day-rally case, not just a theoretical edge) made `rs` NaN via
    # the replace(0, nan) above, so RSI went NaN instead of the
    # conventional 100 (no losses at all = maximally overbought). The
    # degenerate case where avg_gain is ALSO 0 (price dead flat the whole
    # window) is set to a neutral 50 rather than left NaN.
    no_loss = avg_loss == 0
    rsi = rsi.mask(no_loss & (avg_gain > 0), 100.0)
    rsi = rsi.mask(no_loss & (avg_gain == 0), 50.0)

    df["RSI"] = rsi
    return df


# ─────────────────────────────────────────────────────────────────────────────
# MACD
# ─────────────────────────────────────────────────────────────────────────────

def add_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD, Signal Line, and Histogram."""
    ema_fast          = df["Close"].ewm(span=fast,   adjust=False).mean()
    ema_slow          = df["Close"].ewm(span=slow,   adjust=False).mean()
    df["MACD"]        = ema_fast - ema_slow
    df["MACD_Signal"] = df["MACD"].ewm(span=signal, adjust=False).mean()
    df["MACD_Hist"]   = df["MACD"] - df["MACD_Signal"]
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Bollinger Bands
# ─────────────────────────────────────────────────────────────────────────────

def add_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands: Upper, Middle (SMA), Lower, %B, and Width."""
    middle         = df["Close"].rolling(window=period).mean()
    std            = df["Close"].rolling(window=period).std()
    df["BB_Upper"] = middle + std_dev * std
    df["BB_Middle"]= middle
    df["BB_Lower"] = middle - std_dev * std
    df["BB_Width"] = (df["BB_Upper"] - df["BB_Lower"]) / df["BB_Middle"]
    df["BB_Pct"]   = (df["Close"] - df["BB_Lower"]) / (df["BB_Upper"] - df["BB_Lower"])
    return df


# ─────────────────────────────────────────────────────────────────────────────
# ATR
# ─────────────────────────────────────────────────────────────────────────────

def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Average True Range — Wilder's smoothing (the standard definition).

    FIX IND3 — this used a plain `true_range.rolling(period).mean()`, i.e. a
    simple moving average of True Range. That is NOT the ATR Wilder defined
    and NOT the ATR that TradingView, Zerodha Kite, Angel One or any Indian
    broker's charts display. A simple mean weights the 14th-oldest bar exactly
    as heavily as today's and then drops it off a cliff, so a single wide bar
    makes ATR jump on the way in and jump again 14 bars later on the way out,
    long after the volatility event is over.

    This matters well beyond cosmetics, because ATR is load-bearing across the
    whole app: stop-loss distance (`_compute_entry_levels` in analysis/score.py
    uses 1.2–3.0 × ATR), the volatility-scaled target multiplier, position
    sizing (`shares_from_risk`), the trailing stop (`calc_trailing_stop`), every
    screen's `sl`/`tp` in trading/signals.py, and the Supertrend bands. A stop
    computed from an SMA-ATR is a different rupee number from the one a user
    reads off their broker's chart for the same stock on the same day — the
    app and the broker disagree about the same "2 × ATR" instruction.

    The implementation seeds with the simple average of the first `period`
    True Ranges and then applies Wilder's recursion
        ATR_i = (ATR_{i-1} × (period − 1) + TR_i) / period
    which is exactly TradingView's reference implementation (their `rma()`),
    rather than the `ewm(adjust=False)` first-value seed used elsewhere in this
    repo (analysis/trend_quality_score.py's `_compute_adx`) — that seed differs
    for the first few dozen bars.

    The NaN warm-up is deliberately unchanged: rows 0 … period-2 stay NaN
    exactly as the old rolling mean left them. add_supertrend() below depends
    on that (it locates the first non-NaN band row to seed its recursion — see
    FIX IND2(c)), so preserving the warm-up shape keeps that fix intact.
    """
    high_low   = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close  = (df["Low"]  - df["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)

    if len(true_range) >= period:
        # Wilder's seed = SMA of the first `period` True Ranges, placed at row
        # period-1; everything before it stays NaN (same warm-up as before).
        # Feeding that seeded series through ewm(adjust=False) then reproduces
        # Wilder's recursion exactly, vectorised.
        seeded = true_range.copy()
        seed   = float(true_range.iloc[:period].mean())
        seeded.iloc[:period - 1] = np.nan
        seeded.iloc[period - 1]  = seed
        atr = seeded.ewm(alpha=1.0 / period, adjust=False, ignore_na=True).mean()
    else:
        atr = pd.Series(np.nan, index=df.index, dtype=float)

    df["ATR"]     = atr
    df["ATR_Pct"] = df["ATR"] / df["Close"].replace(0, np.nan) * 100  # ATR as % of price
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Anchored VWAP  (intraday — resets at market open each day)
# ─────────────────────────────────────────────────────────────────────────────

def add_anchored_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute intraday VWAP anchored from the start of each trading day.

    Designed for intraday DataFrames with a DatetimeIndex.
    Groups bars by date (ignoring time) and computes cumulative VWAP
    that resets at 09:15 each morning.

    Columns added:
        AVWAP        : anchored VWAP (resets each day)
        AVWAP_Pct    : (Close − AVWAP) / AVWAP × 100
        AVWAP_SD1_Upper : VWAP + 1σ (volume-weighted)
        AVWAP_SD1_Lower : VWAP − 1σ
        AVWAP_SD2_Upper : VWAP + 2σ
        AVWAP_SD2_Lower : VWAP − 2σ
    """
    try:
        idx = df.index
        # Works with both DatetimeIndex and date-based index
        if hasattr(idx, "date"):
            dates = pd.Series(idx.date, index=idx)
        else:
            dates = pd.Series(idx, index=idx)

        # Real feeds (Yahoo 5m) often print Volume=0 or NaN on the session's
        # opening bar(s). NaN volume would poison the running sums for the whole
        # day, and zero volume leaves cum_vol == 0 — either way the old zero-
        # initialised arrays plotted AVWAP = 0 (vertical spikes to zero).
        # Treat bad volume as 0 and leave undefined bars as NaN instead.
        vol    = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0).clip(lower=0.0)
        tp     = (df["High"] + df["Low"] + df["Close"]) / 3
        tp_vol = (tp * vol).fillna(0.0)
        tp_sq  = (tp ** 2 * vol).fillna(0.0)

        avwap_vals = np.full(len(df), np.nan)
        sd1u = np.full(len(df), np.nan)
        sd1l = np.full(len(df), np.nan)
        sd2u = np.full(len(df), np.nan)
        sd2l = np.full(len(df), np.nan)

        cum_tp_vol = 0.0
        cum_vol    = 0.0
        cum_sq     = 0.0
        prev_date  = None

        for i, (idx_val, date) in enumerate(dates.items()):
            if date != prev_date:
                # New day — reset cumulative sums
                cum_tp_vol = 0.0
                cum_vol    = 0.0
                cum_sq     = 0.0
                prev_date  = date

            cum_tp_vol += float(tp_vol.iloc[i])
            cum_vol    += float(vol.iloc[i])
            cum_sq     += float(tp_sq.iloc[i])

            if cum_vol > 0:
                vwap_val = cum_tp_vol / cum_vol
                var_val  = max(0, cum_sq / cum_vol - vwap_val ** 2)
                sd_val   = var_val ** 0.5
                avwap_vals[i] = vwap_val
                sd1u[i]  = vwap_val + 1 * sd_val
                sd1l[i]  = vwap_val - 1 * sd_val
                sd2u[i]  = vwap_val + 2 * sd_val
                sd2l[i]  = vwap_val - 2 * sd_val

        df["AVWAP"]          = avwap_vals
        df["AVWAP_Pct"]      = (df["Close"] / df["AVWAP"].replace(0, np.nan) - 1) * 100
        df["AVWAP_SD1_Upper"] = sd1u
        df["AVWAP_SD1_Lower"] = sd1l
        df["AVWAP_SD2_Upper"] = sd2u
        df["AVWAP_SD2_Lower"] = sd2l

    except Exception as e:
        _log.debug("add_avwap: calculation failed, filling NaN columns: %s", e)
        # Fallback: add NaN columns so downstream code doesn't break
        for col in ["AVWAP", "AVWAP_Pct", "AVWAP_SD1_Upper", "AVWAP_SD1_Lower",
                    "AVWAP_SD2_Upper", "AVWAP_SD2_Lower"]:
            df[col] = np.nan

    return df


# ─────────────────────────────────────────────────────────────────────────────
# VWAP  (rolling 20-day on daily bars — institutional fair-value reference)
# ─────────────────────────────────────────────────────────────────────────────

def add_vwap(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """
    Rolling VWAP on daily OHLCV bars.

    Standard VWAP resets intraday; on daily data we use a 20-day rolling window
    as the medium-term institutional benchmark price.

    Also computes VWAP_Pct = how far current close is from VWAP (in %).
    """
    tp               = (df["High"] + df["Low"] + df["Close"]) / 3
    tp_vol           = tp * df["Volume"]
    # Guard against zero-volume windows (halted stocks / holidays)
    _vol_sum = df["Volume"].rolling(period).sum().replace(0, float("nan"))
    df["VWAP_20"]    = tp_vol.rolling(period).sum() / _vol_sum
    df["VWAP_Pct"]   = (df["Close"] / df["VWAP_20"].replace(0, float("nan")) - 1) * 100
    return df


# ─────────────────────────────────────────────────────────────────────────────
# ADX — trend strength
# ─────────────────────────────────────────────────────────────────────────────

def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Average Directional Index (ADX) with DI+ and DI-.

    ADX < 20  = no trend (range / chop)
    ADX 20-25 = weak trend
    ADX > 25  = trending market
    ADX > 40  = strong trend
    """
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]

    # True Range
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)

    # +DM and -DM (mutually exclusive)
    up_move   = high - high.shift()
    down_move = low.shift() - low
    dm_plus   = np.where((up_move > down_move) & (up_move > 0), up_move,  0.0)
    dm_minus  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    dm_plus_s  = pd.Series(dm_plus,  index=df.index)
    dm_minus_s = pd.Series(dm_minus, index=df.index)

    atr14       = tr.ewm(alpha=1/period, adjust=False).mean()
    di_plus     = 100 * dm_plus_s.ewm(alpha=1/period,  adjust=False).mean() / atr14
    di_minus    = 100 * dm_minus_s.ewm(alpha=1/period, adjust=False).mean() / atr14
    dx          = (100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan))
    df["ADX"]      = dx.ewm(alpha=1/period, adjust=False).mean()
    df["DI_Plus"]  = di_plus
    df["DI_Minus"] = di_minus
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Stochastic Oscillator
# ─────────────────────────────────────────────────────────────────────────────

def add_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    """
    Stochastic %K and %D.

    < 20 = oversold   (potential buy)
    > 80 = overbought (potential sell)
    """
    low_n          = df["Low"].rolling(k_period).min()
    high_n         = df["High"].rolling(k_period).max()
    range_n        = (high_n - low_n).replace(0, np.nan)
    df["Stoch_K"]  = 100 * (df["Close"] - low_n) / range_n
    df["Stoch_D"]  = df["Stoch_K"].rolling(d_period).mean()
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Volume Indicators
# ─────────────────────────────────────────────────────────────────────────────

def add_volume_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Volume SMA, Volume Ratio, and On-Balance Volume."""
    # FIX VOL-SELF (audit 2026-10-03 #17): the baseline is the 20 sessions
    # BEFORE each bar. Averaging the bar into its own baseline damped every
    # spike (a 3x day read as ~2.5x) and so under-scored volume breakouts.
    df["Volume_SMA_20"] = df["Volume"].shift(1).rolling(window=20).mean()
    # Guard a zero/NaN 20-day volume mean (halted stock, all-holiday window) so
    # Volume_Ratio is NaN rather than inf — _score_volume() reads this column.
    df["Volume_Ratio"]  = df["Volume"] / df["Volume_SMA_20"].replace(0, np.nan)

    # FIX IND4 — On-Balance Volume was a pure-Python bar-by-bar loop. It is
    # mathematically just a cumulative sum of signed volume, so the loop bought
    # nothing (the "avoids float precision issues" note it carried is not true:
    # both forms accumulate the same float64 additions in the same order) while
    # costing one Python-level iteration per bar per stock. On the full-universe
    # scan path — ~1,400 tickers × ~500 daily bars — that is ~700k interpreter
    # iterations per scan, on a code path whose runtime budget already had to be
    # defended with a circuit breaker (see data/fetcher.py FIX SPEED2).
    # analysis/trend_quality_score.py already computes OBV this vectorised way;
    # this makes the two agree.
    signed_vol = np.sign(df["Close"].diff()).fillna(0.0) * df["Volume"].fillna(0.0)
    df["OBV"] = signed_vol.cumsum()
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Relative Strength vs Index  (D-2 — delivery trader quality filter)
# ─────────────────────────────────────────────────────────────────────────────

def add_relative_strength(
    df:       pd.DataFrame,
    bench_df: pd.DataFrame,
    period:   int = 63,     # 63 trading days ≈ 3 months (IBD convention)
) -> pd.DataFrame:
    """
    Compute Relative Strength of the stock vs a benchmark (usually Nifty 50).

    RS Line  = stock_close / benchmark_close  (ratio)
    RS_Pct   = (RS now / RS_N_bars_ago − 1) × 100  → +% = outperforming
    RS_Score = percentile rank within own 52-week range [0–100]
               (similar to IBD RS Rating — higher = stronger relative performer)

    Typical usage (delivery trading rule):
        - Only buy stocks where RS_Score ≥ 70 (top 30% relative performers)
        - RS_Line making new highs before price = leading indicator

    Args:
        df       : stock daily OHLCV with Close column
        bench_df : benchmark daily OHLCV (e.g. Nifty 50 from fetch_single('^NSEI'))
        period   : lookback for RS_Pct momentum (default 63 bars = 3 months)

    Columns added:
        RS_Line   : stock / benchmark ratio
        RS_Pct    : N-period relative momentum vs benchmark  (%)
        RS_Score  : 0-100 rank within 52-week RS range
        RS_Trend  : 'outperforming' | 'underperforming' | 'inline'
    """
    try:
        # Align on common dates
        common_idx = df.index.intersection(bench_df.index)
        if len(common_idx) < period + 10:
            for col in ["RS_Line", "RS_Pct", "RS_Score", "RS_Trend"]:
                df[col] = np.nan
            return df

        # FIX RS-ZERO (2026-09-24) — the benchmark often lacks the stock's
        # latest bar (Nifty feed lags a session / provider tiers disagree).
        # Computing the ratio only on the intersection left the stock's last
        # bar NaN, and the percentile lambda below compared NaN against
        # history (all False) -> 0.0, silently costing up to 10/25 momentum
        # points. The benchmark is now forward-filled onto the stock's index
        # for AT MOST 2 bars (documented tolerance for a lagging feed); if it
        # is staler than that, RS stays NaN and RS_Score is NaN
        # ("unavailable" -> score.py falls back to abs-only momentum), never 0.
        bench_close = (bench_df["Close"]
                       .reindex(df.index.union(bench_df.index))
                       .ffill(limit=2)
                       .reindex(df.index))
        bench_close = bench_close.where(df.index >= bench_df.index.min())

        rs_aligned = df["Close"] / bench_close.replace(0, np.nan)
        df["RS_Line"] = rs_aligned

        # RS momentum: N-period change in RS ratio
        rs_pct = rs_aligned.pct_change(period) * 100
        df["RS_Pct"] = rs_pct.round(2)

        # RS Score: 0-100 percentile rank in 252-bar rolling window
        def _pct_rank(series: pd.Series, window: int = 252) -> pd.Series:
            def _rank_last(x):
                # NaN current value -> NaN (unavailable), never 0.0.
                cur = x[-1]
                if np.isnan(cur):
                    return np.nan
                hist = x[:-1]
                hist = hist[~np.isnan(hist)]
                if hist.size == 0:
                    return np.nan
                return (cur > hist).mean() * 100
            return series.rolling(window, min_periods=window // 4).apply(
                _rank_last, raw=True
            )
        df["RS_Score"] = _pct_rank(rs_aligned, 252).round(1)

        # Trend label
        conditions = [
            rs_pct > 2,
            rs_pct < -2,
        ]
        df["RS_Trend"] = np.select(conditions, ["outperforming", "underperforming"],
                                   default="inline")

    except Exception as e:
        _log.debug("add_relative_strength: calculation failed, filling NaN columns: %s", e)
        for col in ["RS_Line", "RS_Pct", "RS_Score", "RS_Trend"]:
            df[col] = np.nan

    return df


# ─────────────────────────────────────────────────────────────────────────────
# Returns / Volatility
# ─────────────────────────────────────────────────────────────────────────────

def add_returns(df: pd.DataFrame) -> pd.DataFrame:
    """Log and simple returns at various horizons."""
    df["Return_1d"]     = df["Close"].pct_change(1)
    df["Return_5d"]     = df["Close"].pct_change(5)
    df["Return_20d"]    = df["Close"].pct_change(20)
    df["Log_Return"]    = np.log(df["Close"] / df["Close"].shift(1))
    df["Volatility_20d"]= df["Log_Return"].rolling(20).std() * np.sqrt(252)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Candlestick Patterns  (from candlestick-patterns skill)
# ─────────────────────────────────────────────────────────────────────────────

def detect_candlestick_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Detect 8 high-reliability candlestick patterns.

    Pattern columns (int): 0 = no pattern, 1 = pattern present.
    Context checks (uptrend / downtrend / support) are done in the signal layer.

    Patterns:
        Pat_Doji          — indecision (body < 5% of range)
        Pat_Hammer        — bullish reversal at bottoms
        Pat_ShootingStar  — bearish reversal at tops
        Pat_BullMarubozu  — strong bullish bar (body ≥ 90% of range)
        Pat_BearMarubozu  — strong bearish bar
        Pat_BullEngulfing — prev red fully engulfed by curr green  ★★★★★
        Pat_BearEngulfing — prev green fully engulfed by curr red  ★★★★★
        Pat_MorningStar   — 3-candle bullish reversal              ★★★★★
        Pat_EveningStar   — 3-candle bearish reversal              ★★★★★
    """
    if "Open" not in df.columns:
        # Some data sources may lack Open; skip silently
        for col in ["Pat_Doji", "Pat_Hammer", "Pat_ShootingStar",
                    "Pat_BullMarubozu", "Pat_BearMarubozu",
                    "Pat_BullEngulfing", "Pat_BearEngulfing",
                    "Pat_MorningStar", "Pat_EveningStar"]:
            df[col] = 0
        return df

    o = df["Open"]
    h = df["High"]
    l = df["Low"]
    c = df["Close"]

    body       = (c - o).abs()
    total_rng  = (h - l).replace(0, np.nan)
    upper_wick = h - pd.concat([c, o], axis=1).max(axis=1)
    lower_wick = pd.concat([c, o], axis=1).min(axis=1) - l

    # Doji: body < 5% of total range
    df["Pat_Doji"] = (body < 0.05 * total_rng).fillna(0).astype(int)

    # Hammer: lower wick ≥ 2× body, tiny upper wick, body in upper third
    body_safe = body.replace(0, np.nan)
    df["Pat_Hammer"] = (
        (lower_wick >= 2.0 * body_safe) &
        (upper_wick <= 0.25 * body_safe)
    ).fillna(False).astype(int)

    # Shooting Star: upper wick ≥ 2× body, tiny lower wick
    df["Pat_ShootingStar"] = (
        (upper_wick >= 2.0 * body_safe) &
        (lower_wick <= 0.25 * body_safe)
    ).fillna(False).astype(int)

    # Marubozu: body ≥ 90% of total range
    marubozu = (body >= 0.90 * total_rng).fillna(False)
    df["Pat_BullMarubozu"] = (marubozu & (c >= o)).astype(int)
    df["Pat_BearMarubozu"] = (marubozu & (c <  o)).astype(int)

    # Bullish Engulfing: prev red → curr green that fully engulfs
    prev_o = o.shift(1)
    prev_c = c.shift(1)
    df["Pat_BullEngulfing"] = (
        (prev_c < prev_o) &   # prev red
        (c      > o     ) &   # curr green
        (c      > prev_o) &   # curr close above prev open
        (o      < prev_c)     # curr open below prev close
    ).fillna(False).astype(int)

    # Bearish Engulfing: prev green → curr red that fully engulfs
    df["Pat_BearEngulfing"] = (
        (prev_c > prev_o) &   # prev green
        (c      < o     ) &   # curr red
        (c      < prev_o) &   # curr close below prev open
        (o      > prev_c)     # curr open above prev close
    ).fillna(False).astype(int)

    # Morning Star (3-candle bullish reversal)
    o2, c2 = o.shift(2), c.shift(2)
    o1, c1 = o.shift(1), c.shift(1)
    body2  = (c2 - o2).abs()
    body1  = (c1 - o1).abs()
    df["Pat_MorningStar"] = (
        (c2 < o2)                &   # bar-2: red
        (body1 < 0.4 * body2)   &   # bar-1: small body (star)
        (c  > o)                &   # bar-0: green
        (c  > (o2 + c2) / 2)        # bar-0 closes above midpoint of bar-2
    ).fillna(False).astype(int)

    # Evening Star (3-candle bearish reversal)
    df["Pat_EveningStar"] = (
        (c2 > o2)                &   # bar-2: green
        (body1 < 0.4 * body2)   &   # bar-1: small body (star)
        (c  < o)                &   # bar-0: red
        (c  < (o2 + c2) / 2)        # bar-0 closes below midpoint of bar-2
    ).fillna(False).astype(int)

    return df


# ─────────────────────────────────────────────────────────────────────────────
# RSI Divergence  (from rsi-divergence skill)
# ─────────────────────────────────────────────────────────────────────────────

def detect_rsi_divergence(df: pd.DataFrame, swing_lookback: int = 20) -> pd.DataFrame:
    """
    Detect bullish and bearish RSI divergence on daily bars.

    Bullish divergence  : price makes new swing low  BUT RSI makes higher low
                          → selling pressure fading, potential reversal up
    Bearish divergence  : price makes new swing high BUT RSI makes lower high
                          → buying pressure fading, potential reversal down

    Only flags divergence when RSI is in a meaningful zone:
        Bullish  — RSI must be < 45 (oversold / approaching oversold)
        Bearish  — RSI must be > 55 (overbought / approaching overbought)

    FIX IND5 — this was a Python loop over every bar, each iteration doing four
    numpy reductions over a 20-bar slice. Same cost problem as the OBV loop
    (see FIX IND4), but worse per bar, and it is the reason "divergence" could
    not be afforded in analysis/score.py's screening indicator subset — which
    in turn is why patterns/divergence silently never appeared in any score
    narrative (see FIX SCORE-PAT in analysis/score.py). Rewritten with
    sliding_window_view: the same windows are materialised once as a single
    (n − lookback + 1, lookback) view and reduced along axis 1 in C. The
    detection rules below are unchanged, bar for bar.
    """
    if "RSI" not in df.columns or len(df) < swing_lookback + 5:
        df["RSI_Bull_Div"] = 0
        df["RSI_Bear_Div"] = 0
        return df

    n        = len(df)
    bull_div = np.zeros(n, dtype=int)
    bear_div = np.zeros(n, dtype=int)

    prices = np.asarray(df["Close"], dtype=float)
    rsis   = np.asarray(df["RSI"],   dtype=float)

    # Row j of these views is prices[j : j + swing_lookback], which is exactly
    # the window the old loop used for bar i = j + swing_lookback.
    win_p = sliding_window_view(prices, swing_lookback)[: n - swing_lookback]
    win_r = sliding_window_view(rsis,   swing_lookback)[: n - swing_lookback]

    # Bars the old loop covered: i = swing_lookback … n-1.
    curr_p = prices[swing_lookback:]
    curr_r = rsis[swing_lookback:]

    # An all-NaN window would make nanmin/nanargmin raise (the old loop would
    # have raised too); skip those rows rather than change behaviour silently.
    usable = ~np.isnan(win_p).all(axis=1) & ~np.isnan(curr_r)
    if not usable.any():
        df["RSI_Bull_Div"] = bull_div
        df["RSI_Bear_Div"] = bear_div
        return df

    rows      = np.flatnonzero(usable)
    wp, wr    = win_p[rows], win_r[rows]
    cp, cr    = curr_p[rows], curr_r[rows]
    positions = rows + swing_lookback          # index back into the full series

    low_idx   = np.nanargmin(wp, axis=1)
    high_idx  = np.nanargmax(wp, axis=1)
    row_range = np.arange(len(rows))
    rsi_at_low  = wr[row_range, low_idx]
    rsi_at_high = wr[row_range, high_idx]

    # Bullish: price at/near the window low but RSI meaningfully higher than it
    # was at that low → selling pressure fading.
    bull = (
        (cr < 45)
        & (cp <= np.nanmin(wp, axis=1) * 1.01)
        & ~np.isnan(rsi_at_low)
        & (cr > rsi_at_low + 2)
    )
    # Bearish: price at/near the window high but RSI lower than at that high.
    bear = (
        (cr > 55)
        & (cp >= np.nanmax(wp, axis=1) * 0.99)
        & ~np.isnan(rsi_at_high)
        & (cr < rsi_at_high - 2)
    )

    bull_div[positions[bull]] = 1
    bear_div[positions[bear]] = 1

    df["RSI_Bull_Div"] = bull_div
    df["RSI_Bear_Div"] = bear_div
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Supertrend  (intraday + delivery — India's most-used trend indicator)
# ─────────────────────────────────────────────────────────────────────────────

def add_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """
    Supertrend indicator — ATR-based dynamic support/resistance band.

    Classic settings: period=10, multiplier=3.0 (works for daily and intraday)

    How it works:
        Upper Band = (High+Low)/2 + multiplier × ATR
        Lower Band = (High+Low)/2 − multiplier × ATR
        Supertrend switches direction when price crosses a band.

    Columns added:
        ST_Upper       : raw upper band value
        ST_Lower       : raw lower band value
        Supertrend     : current active support/resistance value
        ST_Direction   : 1 = bullish (price above band), -1 = bearish
        ST_Signal      : +1 = just crossed up (buy), -1 = just crossed down (sell)

    FIX IND2: three bugs fixed here, all verified against synthetic OHLC
    series with controlled ATR (see indicators.py module docstring for
    the full writeup):
      (c) — the dominant one — upper_band/lower_band used to be seeded
          directly from upper_raw/lower_raw, whose row 0 is ALWAYS NaN
          for any real series (ATR needs `period` rows of warm-up). The
          tightening recursion below can never recover from a NaN
          previous value (any comparison against NaN is False), so that
          NaN silently propagated forward through the ENTIRE rest of the
          series, every time, for every stock — not a rare edge case.
          Fixed by seeding the recursion at the first bar where the raw
          bands are actually valid, instead of at a row that's
          structurally guaranteed to be NaN.
      (a) supertrend[i]/direction[i] are now seeded at that first valid
          bar BEFORE the loop starts, not after — the loop's first real
          iteration reads the previous bar's supertrend value as its
          `prev_st`, and a too-late assignment leaves prev_st at an
          uninitialized default for that critical first comparison. This
          caused obvious downtrends to flash a false bullish flip for
          several bars at the start.
      (b) the trend-flip check compares against the PRIOR bar's band
          (upper_band[i-1] / lower_band[i-1]) rather than the current
          bar's just-tightened band — matching the standard Supertrend
          definition (TradingView's reference implementation).

    ST_Direction is 0 (not a fake +1) for the unavoidable ATR warm-up
    bars at the start of any series — distinguishable from a real -1/+1
    reading — and ST_Signal is suppressed on the first real bar (there's
    no prior trend yet to have "flipped" from).
    """
    if "ATR" not in df.columns:
        df = add_atr(df, period=period)

    hl2   = (df["High"] + df["Low"]) / 2
    atr   = df["ATR"]
    upper_raw = hl2 + multiplier * atr
    lower_raw = hl2 - multiplier * atr

    # Trailing band arrays
    n           = len(df)
    upper_band  = upper_raw.values.copy()
    lower_band  = lower_raw.values.copy()
    supertrend  = np.full(n, np.nan)
    direction   = np.zeros(n, dtype=int)   # 0 = undefined (warm-up); 1 = bullish, -1 = bearish
    close       = df["Close"].values

    # FIX IND2(c): find the first bar where the raw ATR-based bands are
    # actually valid — seeding from row 0 directly is unsafe since ATR's
    # rolling window guarantees row 0 is NaN.
    valid_mask = ~np.isnan(upper_raw.values)
    if not valid_mask.any():
        # Not even one bar of valid ATR (series shorter than `period`) —
        # return honest NaN/undefined columns rather than guessing.
        df["ST_Upper"]     = upper_band
        df["ST_Lower"]     = lower_band
        df["Supertrend"]   = supertrend
        df["ST_Direction"] = direction
        df["ST_Signal"]    = 0
        return df

    start = int(np.argmax(valid_mask))

    # FIX IND2(a): seed BEFORE the loop runs, at the first real bar — not
    # row 0 (which is guaranteed NaN) and not after the loop (too late
    # for the loop's first iteration to see it).
    supertrend[start] = upper_band[start]
    direction[start]  = -1

    for i in range(start + 1, n):
        # Upper band: only move DOWN (tighten)
        if upper_raw.iloc[i] < upper_band[i - 1] or close[i - 1] > upper_band[i - 1]:
            upper_band[i] = upper_raw.iloc[i]
        else:
            upper_band[i] = upper_band[i - 1]

        # Lower band: only move UP (tighten)
        if lower_raw.iloc[i] > lower_band[i - 1] or close[i - 1] < lower_band[i - 1]:
            lower_band[i] = lower_raw.iloc[i]
        else:
            lower_band[i] = lower_band[i - 1]

        # Direction flipping
        prev_st = supertrend[i - 1]
        if prev_st == upper_band[i - 1]:          # was bearish
            # FIX IND2(b): compare against the PRIOR bar's band (the level
            # actually in force when this bar closed), not the current
            # bar's already-tightened band.
            if close[i] > upper_band[i - 1]:
                direction[i] = 1                   # flipped bullish
                supertrend[i] = lower_band[i]
            else:
                direction[i] = -1
                supertrend[i] = upper_band[i]
        else:                                      # was bullish
            if close[i] < lower_band[i - 1]:
                direction[i] = -1                  # flipped bearish
                supertrend[i] = upper_band[i]
            else:
                direction[i] = 1
                supertrend[i] = lower_band[i]

    df["ST_Upper"]     = upper_band
    df["ST_Lower"]     = lower_band
    df["Supertrend"]   = supertrend
    df["ST_Direction"] = direction

    # Signal: +1 on bar where direction flips from -1 to +1, -1 vice versa.
    # Suppressed at `start` itself — no prior trend exists yet to flip from.
    dir_series      = pd.Series(direction, index=df.index)
    diff            = dir_series.diff()
    signal          = (diff > 0).astype(int) - (diff < 0).astype(int)
    signal.iloc[start] = 0
    df["ST_Signal"] = signal
    return df


# ─────────────────────────────────────────────────────────────────────────────
# CPR — Central Pivot Range  (India's #1 intraday framework)
# ─────────────────────────────────────────────────────────────────────────────

def add_pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """
    Classic Pivot Points + Central Pivot Range (CPR) based on the *previous* bar.

    Works on any timeframe:
        Daily bars   → daily pivots from previous day's OHLC
        Weekly bars  → weekly pivots from previous week
        Intraday bars → pivots shift forward one BAR (approximate daily CPR)

    Levels computed:
        Pivot = (Prev_High + Prev_Low + Prev_Close) / 3
        BC    = (Prev_High + Prev_Low) / 2              ← Bottom of CPR
        TC    = Pivot + (Pivot - BC)                     ← Top of CPR
        R1    = 2 × Pivot − Prev_Low
        R2    = Pivot + (Prev_High − Prev_Low)
        R3    = Prev_High + 2 × (Pivot − Prev_Low)
        S1    = 2 × Pivot − Prev_High
        S2    = Pivot − (Prev_High − Prev_Low)
        S3    = Prev_Low  − 2 × (Prev_High − Pivot)

    Additional columns:
        CPR_Width   : TC − BC  (narrow CPR = directional day, wide = choppy)
        CPR_Width_Pct : CPR_Width / Pivot × 100
        Price_vs_CPR : 'above', 'inside', 'below'
    """
    ph = df["High"].shift(1)
    pl = df["Low"].shift(1)
    pc = df["Close"].shift(1)

    pivot = (ph + pl + pc) / 3
    bc    = (ph + pl) / 2
    tc    = pivot + (pivot - bc)

    df["Pivot"] = pivot
    df["CPR_BC"] = bc
    df["CPR_TC"] = tc
    df["R1"] = 2 * pivot - pl
    df["R2"] = pivot + (ph - pl)
    df["R3"] = ph  + 2 * (pivot - pl)
    df["S1"] = 2 * pivot - ph
    df["S2"] = pivot - (ph - pl)
    df["S3"] = pl   - 2 * (ph - pivot)

    cpr_width = (tc - bc).abs()
    df["CPR_Width"]     = cpr_width
    df["CPR_Width_Pct"] = cpr_width / pivot * 100

    # Classify current close vs CPR band
    close = df["Close"]
    cpr_upper = pd.concat([tc, bc], axis=1).max(axis=1)
    cpr_lower = pd.concat([tc, bc], axis=1).min(axis=1)

    conditions = [
        close > cpr_upper,
        (close >= cpr_lower) & (close <= cpr_upper),
        close < cpr_lower,
    ]
    df["Price_vs_CPR"] = np.select(conditions, ["above", "inside", "below"], default="unknown")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Fibonacci Retracement Levels  (from fibonacci-trading skill)
# ─────────────────────────────────────────────────────────────────────────────

def add_fibonacci_levels(df: pd.DataFrame, lookback: int = 252) -> pd.DataFrame:
    """
    Fibonacci retracement levels from rolling swing high / swing low.

    Levels are measured as retracement % from swing high toward swing low:

        Fib_23_6 = H − 0.236 × (H − L)   ← shallow pullback
        Fib_38_2 = H − 0.382 × (H − L)   ★ key support / entry zone
        Fib_50_0 = H − 0.500 × (H − L)   ← mid range
        Fib_61_8 = H − 0.618 × (H − L)   ★ golden-ratio support (deepest valid)
        Fib_78_6 = H − 0.786 × (H − L)   ← last-ditch support before new lows

    Price ordering (high → low):
        Fib_23_6 > Fib_38_2 > Fib_50_0 > Fib_61_8 > Fib_78_6

    Two lookbacks are calculated:
        Long  (default 252 bars ≈ 1 year)   → columns  Fib_High, Fib_Low, Fib_*
        Short (50 bars  ≈ 2.5 months)       → columns  Fib50_38_2, Fib50_50_0, Fib50_61_8

    Proximity signals (long-term levels, ±1.5% tolerance):
        Fib_Near_38 : 1 if Close is within 1.5 % of the 38.2 % level
        Fib_Near_62 : 1 if Close is within 1.5 % of the 61.8 % level

    Zone label (long-term):
        Fib_Zone : 'above_23' | '23_38' | '38_50' | '50_62' | '62_78' | 'below_78'
    """
    # ── Long-term swing (major levels) ──────────────────────────────────────
    min_p = max(50, lookback // 5)
    h   = df["High"].rolling(window=lookback, min_periods=min_p).max()
    l   = df["Low"].rolling(window=lookback, min_periods=min_p).min()
    rng = (h - l).replace(0, np.nan)

    df["Fib_High"] = h
    df["Fib_Low"]  = l
    df["Fib_23_6"] = h - 0.236 * rng
    df["Fib_38_2"] = h - 0.382 * rng
    df["Fib_50_0"] = h - 0.500 * rng
    df["Fib_61_8"] = h - 0.618 * rng
    df["Fib_78_6"] = h - 0.786 * rng

    # ── Short-term swing (50-bar pullback levels) ────────────────────────────
    h50  = df["High"].rolling(window=50, min_periods=20).max()
    l50  = df["Low"].rolling(window=50, min_periods=20).min()
    r50  = (h50 - l50).replace(0, np.nan)

    df["Fib50_38_2"] = h50 - 0.382 * r50
    df["Fib50_50_0"] = h50 - 0.500 * r50
    df["Fib50_61_8"] = h50 - 0.618 * r50

    # ── Proximity flags (long-term, ±1.5% band) ─────────────────────────────
    tol = 0.015
    df["Fib_Near_38"] = (
        ((df["Close"] - df["Fib_38_2"]).abs() / df["Fib_38_2"]) <= tol
    ).fillna(False).astype(int)

    df["Fib_Near_62"] = (
        ((df["Close"] - df["Fib_61_8"]).abs() / df["Fib_61_8"]) <= tol
    ).fillna(False).astype(int)

    # ── Zone the current close sits in (long-term) ───────────────────────────
    # Fib_23_6 > Fib_38_2 > Fib_50_0 > Fib_61_8 > Fib_78_6 (price level order)
    conditions = [
        df["Close"] > df["Fib_23_6"],
        (df["Close"] > df["Fib_38_2"]) & (df["Close"] <= df["Fib_23_6"]),
        (df["Close"] > df["Fib_50_0"]) & (df["Close"] <= df["Fib_38_2"]),
        (df["Close"] > df["Fib_61_8"]) & (df["Close"] <= df["Fib_50_0"]),
        (df["Close"] > df["Fib_78_6"]) & (df["Close"] <= df["Fib_61_8"]),
        df["Close"] <= df["Fib_78_6"],
    ]
    zone_labels = ["above_23", "23_38", "38_50", "50_62", "62_78", "below_78"]
    df["Fib_Zone"] = np.select(conditions, zone_labels, default="unknown")

    return df


# ─────────────────────────────────────────────────────────────────────────────
# Indicator group registry (must come AFTER every add_*/detect_* function
# above is defined) — used by add_all_indicators()'s optional `groups` param.
# ─────────────────────────────────────────────────────────────────────────────

_INDICATOR_GROUPS = {
    "ma":         add_moving_averages,
    "rsi":        add_rsi,
    "macd":       add_macd,
    "bollinger":  add_bollinger_bands,
    "atr":        add_atr,
    "vwap":       add_vwap,
    "adx":        add_adx,
    "stochastic": add_stochastic,
    "volume":     add_volume_indicators,
    "returns":    add_returns,
    "fibonacci":  add_fibonacci_levels,
    "supertrend": add_supertrend,
    "pivot":      add_pivot_points,
    "patterns":   detect_candlestick_patterns,
    "divergence": detect_rsi_divergence,
}

assert set(_INDICATOR_GROUPS) == set(_ALL_GROUPS), (
    "_INDICATOR_GROUPS and _ALL_GROUPS have drifted apart — every group "
    "name declared near add_all_indicators() must have exactly one "
    "function registered here, and vice versa."
)


# ─────────────────────────────────────────────────────────────────────────────
# Legacy signal generators (used by strategies)
# ─────────────────────────────────────────────────────────────────────────────

def rsi_signal(df: pd.DataFrame, oversold: float = 30, overbought: float = 70) -> pd.Series:
    """Returns +1 (buy), -1 (sell), 0 (hold) based on RSI thresholds."""
    signal = pd.Series(0, index=df.index)
    signal[df["RSI"] < oversold]  = 1
    signal[df["RSI"] > overbought] = -1
    return signal


def macd_crossover_signal(df: pd.DataFrame) -> pd.Series:
    """Returns +1 on bullish MACD crossover, -1 on bearish crossover."""
    signal  = pd.Series(0, index=df.index)
    bullish = (df["MACD"] > df["MACD_Signal"]) & (df["MACD"].shift(1) <= df["MACD_Signal"].shift(1))
    bearish = (df["MACD"] < df["MACD_Signal"]) & (df["MACD"].shift(1) >= df["MACD_Signal"].shift(1))
    signal[bullish] = 1
    signal[bearish] = -1
    return signal


def sma_crossover_signal(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    """Golden/Death cross: +1 when fast SMA crosses above slow SMA."""
    fast_col, slow_col = f"SMA_{fast}", f"SMA_{slow}"
    if fast_col not in df.columns:
        df = add_moving_averages(df)
    signal = pd.Series(0, index=df.index)
    golden = (df[fast_col] > df[slow_col]) & (df[fast_col].shift(1) <= df[slow_col].shift(1))
    death  = (df[fast_col] < df[slow_col]) & (df[fast_col].shift(1) >= df[slow_col].shift(1))
    signal[golden] = 1
    signal[death]  = -1
    return signal
