"""
analysis/momentum_scanner.py — Positional momentum opportunity scanner.

Pure module (no Streamlit, no session state) so it can be called from the
headless alert pipeline (alerts/checks/momentum_scan.py) and unit-tested
offline against a synthetic OHLCV frame.

Recipe (calibrated for NSE positional swings, ~5–30 day holds):

    Trend confirmed:     Close > SMA50 AND SMA50 > SMA200
    Donchian-55 breakout: today's Close > highest Close of the previous 55 sessions
    Volume surge:        today's Volume > 1.5 × 20-day average volume
    RSI(14) in band:     55 ≤ RSI ≤ 75
                         (55 = momentum kicked in; 75 = not exhausted —
                          above 75 the forward return goes negative in
                          published NSE studies)
    Above session VWAP:  last close > VWAP (institutional accumulation ongoing)
    Not exhausted:       today's true range < 2.5 × ATR(20)
                         (avoids parabolic days where entry-to-stop distance
                          becomes 8 %+ and R:R collapses)

Then rank surviving names by relative strength vs Nifty over the last
63 sessions (~3 months) and return the top N (default 8).

Why these choices — see the CLAUDE.md rationale and the answer that shipped
with this file's initial commit for the trade-offs versus 20-day breakout /
2× volume / RSI 40–70 alternatives. Do not tune these in-place without
running the golden regression (tests/test_momentum_scanner.py).

Sector filter deliberately omitted: analysis/sector_classification.py doesn't
cover the full niftytotalmarket universe cleanly, and filtering on unclassified
sector would silently drop good candidates. Ranking by RS-vs-Nifty already
captures the "in a leading regime" bias that sector-rank was meant to add.

Public API:
    scan_momentum(universe, price_history, nifty_history, ...) -> list[dict]

Both universe iteration and history fetching are the CALLER's responsibility
(the alert script provides them via data.fetcher). This module does NOT touch
the network, so it stays fast and testable.
"""
from __future__ import annotations

import html as _html
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

import pandas as pd

_log = logging.getLogger("analysis.momentum_scanner")


# ─────────────────────────────────────────────────────────────────────────────
# Config — tune here (and update tests/test_momentum_scanner.py if you do)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MomentumConfig:
    breakout_lookback: int = 55       # Donchian-55 (Turtle Traders window)
    volume_multiplier: float = 1.5    # today's vol vs 20d avg
    volume_avg_days:  int   = 20
    rsi_period:       int   = 14
    rsi_min:          float = 55.0
    rsi_max:          float = 75.0
    trend_fast_sma:   int   = 50
    trend_slow_sma:   int   = 200
    atr_period:       int   = 20
    exhaustion_mult:  float = 2.5     # today's TR vs ATR(20) — reject if above
    rs_lookback:      int   = 63      # ~3 months for RS ranking
    top_n:            int   = 8       # how many to surface
    min_price:        float = 20.0    # penny-stock guard (₹)
    min_bars_needed:  int   = 210     # need SMA200 + 10 buffer


DEFAULT_CONFIG = MomentumConfig()


# ─────────────────────────────────────────────────────────────────────────────
# Indicators — self-contained, no dependency on utils/indicators.py so this
# module stays trivially usable in the alert workflow.
# ─────────────────────────────────────────────────────────────────────────────

def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI — same convention as utils/indicators.py."""
    delta = close.diff()
    gain  = delta.clip(lower=0.0)
    loss  = -delta.clip(upper=0.0)
    # Wilder smoothing = EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    # A zero avg_loss means no down days in the window — canonical RSI = 100
    # rather than NaN (which would silently propagate through the filter).
    rs = avg_gain / avg_loss.where(avg_loss > 0)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.where(avg_loss > 0, 100.0)


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    """True Range → Wilder ATR."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _rolling_vwap(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """20-session rolling VWAP — a robust proxy for the daily-bars case where
    a true session VWAP requires intraday data we're not fetching here.

    Rationale: at the daily-bar granularity, "above session VWAP" collapses
    to "above today's typical price", which is trivially true or false and
    carries no accumulation signal. A rolling VWAP over ~20 sessions is the
    standard substitute used in positional momentum work — same
    interpretation ("price is above where recent institutional volume
    cleared"), just at daily-bar resolution."""
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    pv = typical * df["Volume"]
    return pv.rolling(window, min_periods=window).sum() / \
           df["Volume"].rolling(window, min_periods=window).sum()


# ─────────────────────────────────────────────────────────────────────────────
# Per-ticker evaluation
# ─────────────────────────────────────────────────────────────────────────────

def _evaluate_ticker(
    ticker: str,
    df: pd.DataFrame,
    nifty_returns: Optional[pd.Series],
    cfg: MomentumConfig,
) -> Optional[Dict]:
    """Apply the six filters and return a candidate dict if all pass.

    Returns None if the ticker fails any filter or has insufficient data —
    caller distinguishes 'rejected by filters' from 'insufficient data'
    only via logging (this scanner is a coarse funnel, not a diagnostic).
    """
    if df is None or df.empty or len(df) < cfg.min_bars_needed:
        return None

    df = df.copy()
    # Yahoo/Stooq occasionally return a Close of 0 or negative on holidays or
    # halted sessions — drop them before any ratio math.
    df = df[df["Close"] > 0].dropna(subset=["Close", "High", "Low", "Volume"])
    if len(df) < cfg.min_bars_needed:
        return None

    close  = df["Close"]
    price  = float(close.iloc[-1])
    if price < cfg.min_price:
        return None

    # 1. Trend
    sma_fast = close.rolling(cfg.trend_fast_sma).mean().iloc[-1]
    sma_slow = close.rolling(cfg.trend_slow_sma).mean().iloc[-1]
    if not (price > sma_fast > sma_slow):
        return None

    # 2. Donchian-55 breakout — today's close breaks the prior N-day high
    #    (exclude today from the lookback so we're comparing against the
    #    prior window, not against itself)
    lookback = cfg.breakout_lookback
    prior_high = close.iloc[-(lookback + 1):-1].max()
    if not (price > prior_high):
        return None

    # 3. Volume surge
    vol_today = float(df["Volume"].iloc[-1])
    vol_avg   = float(df["Volume"].iloc[-(cfg.volume_avg_days + 1):-1].mean())
    if vol_avg <= 0 or vol_today < cfg.volume_multiplier * vol_avg:
        return None

    # 4. RSI in band
    rsi_val = float(_rsi(close, cfg.rsi_period).iloc[-1])
    if not (cfg.rsi_min <= rsi_val <= cfg.rsi_max):
        return None

    # 5. Above rolling VWAP (daily-bar VWAP proxy — see _rolling_vwap docstring)
    vwap_val = float(_rolling_vwap(df).iloc[-1])
    if not (price > vwap_val):
        return None

    # 6. Not exhausted — today's TR vs ATR(20)
    prev_close = float(close.iloc[-2])
    high_today = float(df["High"].iloc[-1])
    low_today  = float(df["Low"].iloc[-1])
    tr_today = max(
        high_today - low_today,
        abs(high_today - prev_close),
        abs(low_today  - prev_close),
    )
    atr_val = float(_atr(df, cfg.atr_period).iloc[-1])
    if atr_val <= 0 or tr_today > cfg.exhaustion_mult * atr_val:
        return None

    # ── Relative strength vs Nifty (for ranking, not filtering) ──
    rs = _relative_strength(close, nifty_returns, cfg.rs_lookback)

    return {
        "ticker": ticker,
        "price":  round(price, 2),
        "prior_high_55d": round(float(prior_high), 2),
        "breakout_pct":   round((price / float(prior_high) - 1) * 100, 2),
        "volume_ratio":   round(vol_today / vol_avg, 2),
        "rsi":            round(rsi_val, 1),
        "vwap":           round(vwap_val, 2),
        "atr":            round(atr_val, 2),
        "sma50":          round(float(sma_fast), 2),
        "sma200":         round(float(sma_slow), 2),
        "rs_63d":         None if rs is None else round(rs, 2),
        # Suggested position math — surface entry / stop / target so the user
        # doesn't need to open another page to size the trade.
        "suggested_entry":  round(price, 2),
        "suggested_stop":   round(price - 2.0 * atr_val, 2),   # 2-ATR stop
        "suggested_target": round(price + 4.0 * atr_val, 2),   # 2R target
    }


def _relative_strength(
    close: pd.Series,
    nifty_returns: Optional[pd.Series],
    window: int,
) -> Optional[float]:
    """Stock's window-return minus Nifty's window-return, in percent.
    Returns None if Nifty data missing or the series is too short."""
    if len(close) < window + 1:
        return None
    stock_ret = float(close.iloc[-1] / close.iloc[-(window + 1)] - 1) * 100
    if nifty_returns is None or len(nifty_returns) < window + 1:
        return stock_ret   # degrade to absolute return — caller can still rank
    nifty_ret = float(nifty_returns.iloc[-1] / nifty_returns.iloc[-(window + 1)] - 1) * 100
    return stock_ret - nifty_ret


# ─────────────────────────────────────────────────────────────────────────────
# Public entry
# ─────────────────────────────────────────────────────────────────────────────

def scan_momentum(
    universe: Iterable[str],
    fetcher: Callable[[str], Optional[pd.DataFrame]],
    nifty_history: Optional[pd.DataFrame] = None,
    cfg: MomentumConfig = DEFAULT_CONFIG,
) -> List[Dict]:
    """Scan `universe` and return up to cfg.top_n momentum candidates.

    Args:
        universe:      iterable of tickers (NSE symbols, no .NS suffix — the
                       caller's fetcher is responsible for that).
        fetcher:       callable ticker -> daily-bars DataFrame with columns
                       [Open, High, Low, Close, Volume]. Return None or empty
                       for tickers that failed to fetch — they're skipped.
        nifty_history: optional Nifty 50 daily bars (Close column used) for
                       RS ranking. If None, candidates are ranked by absolute
                       63-day return instead.
        cfg:           MomentumConfig, defaults tuned for NSE positional.

    Returns:
        list[dict] — top-N candidates ranked by rs_63d desc. Each dict has
        the fields documented in _evaluate_ticker's return.
    """
    nifty_close = nifty_history["Close"] if (
        nifty_history is not None and not nifty_history.empty
    ) else None

    candidates: List[Dict] = []
    n_scanned = 0
    n_no_data = 0
    n_rejected = 0

    for ticker in universe:
        n_scanned += 1
        try:
            df = fetcher(ticker)
        except Exception as _fetch_err:
            _log.debug("scan_momentum: fetch failed for %s: %s", ticker, _fetch_err)
            n_no_data += 1
            continue

        if df is None or df.empty:
            n_no_data += 1
            continue

        result = _evaluate_ticker(ticker, df, nifty_close, cfg)
        if result is None:
            n_rejected += 1
            continue
        candidates.append(result)

    # Rank: RS desc, tie-break on breakout_pct desc, then volume_ratio desc
    candidates.sort(
        key=lambda c: (
            -(c["rs_63d"] if c["rs_63d"] is not None else -9999),
            -c["breakout_pct"],
            -c["volume_ratio"],
        )
    )

    top = candidates[: cfg.top_n]
    _log.info(
        "scan_momentum: scanned=%d, no_data=%d, rejected=%d, passed=%d, returning_top=%d",
        n_scanned, n_no_data, n_rejected, len(candidates), len(top),
    )
    return top


# ─────────────────────────────────────────────────────────────────────────────
# Message formatting — kept here so the alert script stays a thin runner
# ─────────────────────────────────────────────────────────────────────────────

def format_momentum_message(candidates: List[Dict], *, max_lines: int = 8) -> str:
    """Format scan output as an HTML/plain-text alert message.

    HTML-ish (Telegram-parse_mode=HTML friendly) with tags the Gmail path
    strips to plain text — same convention as check_nifty_trend / check_vix
    messages in alerts/check_alerts.py."""
    if not candidates:
        return (
            "📊 <b>Momentum scan</b>\n"
            "No stocks passed today's filters "
            "(55d breakout + 1.5× volume + RSI 55–75 + trend confirmed).\n"
            "Quiet day — no momentum setups met the criteria in this scan."
        )

    lines: List[str] = [
        "🚀 <b>Momentum setups</b>",
        f"<i>{len(candidates)} setup(s) surfaced — top {min(max_lines, len(candidates))} below</i>",
        "",
    ]
    for i, c in enumerate(candidates[:max_lines], start=1):
        rs_str = f"RS {c['rs_63d']:+.1f}%" if c["rs_63d"] is not None else "RS n/a"
        lines.append(
            f"{i}. <b>{_html.escape(str(c['ticker']), quote=False)}</b> — ₹{c['price']:,.2f} "
            f"(broke 55d high +{c['breakout_pct']:.1f}%, vol {c['volume_ratio']:.1f}×, "
            f"RSI {c['rsi']:.0f}, {rs_str})"
        )
        lines.append(
            f"   Entry ₹{c['suggested_entry']:,.2f} · "
            f"Stop ₹{c['suggested_stop']:,.2f} (2-ATR) · "
            f"Target ₹{c['suggested_target']:,.2f} (2R)"
        )
    lines.extend([
        "",
        "<i>Descriptive scan — not advice. Levels are rule-derived reference points. "
        "Filters: trend + Donchian-55 + volume + RSI + VWAP + ATR-exhaustion.</i>",
    ])
    return "\n".join(lines)


__all__ = [
    "MomentumConfig",
    "DEFAULT_CONFIG",
    "scan_momentum",
    "format_momentum_message",
]
