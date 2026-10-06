"""analysis/edge_flags.py — trading-status caveats for one stock (display only).

Audit 2026-10-03 #20: a 15-day-old listing got a definite score, a stock with
no trades for days read "Neutral", and nothing warned when a stock was
circuit-locked or trade-to-trade. These flags are descriptive context shown
beside the score; they never change it.

Pure: no Streamlit, no network. Inputs are the daily OHLCV frame and the
security info from data.nse_security_info.get_security_info().
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd

_FULL_HISTORY_SESSIONS = 250      # 200-DMA and 52-week levels need ~a year
_SHORT_HISTORY_SESSIONS = 60
_STALE_SESSIONS = 3               # last bar this many sessions behind = no trades
_CIRCUIT_TOL_PCT = 0.15           # within 0.15 pp of the band edge counts as at the limit

_SERIES_NOTES = {
    "BE": ("amber", "Trade-to-trade (BE series)",
           "Every trade settles by delivery — intraday trading isn't allowed."),
    "BZ": ("red", "Non-compliant company (BZ series)",
           "Trade-to-trade for companies that haven't met listing requirements; "
           "liquidity is usually thin."),
    "SM": ("amber", "SME platform (SM series)", "SME stocks trade in large minimum lots with thin liquidity."),
    "ST": ("amber", "SME platform (ST series)", "SME trade-to-trade; large minimum lots, thin liquidity."),
}


@dataclass(frozen=True)
class EdgeFlag:
    key: str
    tone: str          # "red" | "amber" | "info"
    label: str
    detail: str


def _sessions_between(a: _dt.date, b: _dt.date) -> int:
    """Trading sessions strictly after a up to and including b.

    Only counts that matter here are under a year; anything older than ~400
    calendar days returns early (a 1995 listing must not walk 30 years of
    calendar, or warn about every year the holiday table doesn't cover)."""
    if b <= a:
        return 0
    if (b - a).days > 400:
        return _FULL_HISTORY_SESSIONS + (b - a).days      # comfortably "a year or more"
    try:
        from utils.nse_holidays import is_trading_day
    except Exception:                      # calendar unavailable: weekdays only
        def is_trading_day(d):
            return d.weekday() < 5
    n, d = 0, a + _dt.timedelta(days=1)
    while d <= b:
        n += bool(is_trading_day(d))
        d += _dt.timedelta(days=1)
    return n


def edge_flags(df: Optional[pd.DataFrame], info: Optional[Dict] = None,
               today: Optional[_dt.date] = None) -> List[EdgeFlag]:
    """Caveats for the latest bar of a DAILY frame. Never raises."""
    info = info or {}
    # IST, not the server clock: Streamlit Cloud runs in UTC, so 00:00-05:30
    # IST would otherwise count from yesterday.
    today = today or (_dt.datetime.now(_dt.timezone.utc)
                      + _dt.timedelta(hours=5, minutes=30)).date()
    flags: List[EdgeFlag] = []
    try:
        flags += _series_flags(info)
        flags += _history_flags(df, info, today)
        flags += _stale_flags(df, today)
        flags += _circuit_flags(df, info)
    except Exception:                      # flags are context; never break a page
        pass
    order = {"red": 0, "amber": 1, "info": 2}
    return sorted(flags, key=lambda f: order.get(f.tone, 3))


# Flags that keep a stock off ranked lists (Top Picks, Tomorrow's Watchlist,
# screener ranking). The score still shows on Analyze Stock beside the red
# flag. A circuit only excludes when locked (high == low) on the latest bar.
_LIST_EXCLUDED = {
    "new_listing": "new listing (under 60 sessions)",
    "series_bz": "BZ series",
    "upper_circuit": "locked at upper circuit",
    "lower_circuit": "locked at lower circuit",
}


def list_exclusion(df: Optional[pd.DataFrame], info: Optional[Dict] = None,
                   today: Optional[_dt.date] = None) -> Optional[str]:
    """Why a stock is kept off ranked lists, or None. Never raises."""
    for f in edge_flags(df, info, today):
        if f.key.endswith("_circuit") and f.tone != "red":
            continue                       # at the limit but still trading
        if f.key in _LIST_EXCLUDED:
            return _LIST_EXCLUDED[f.key]
    return None


def _series_flags(info: Dict) -> List[EdgeFlag]:
    series = str(info.get("series") or "").upper()
    if series in _SERIES_NOTES:
        tone, label, detail = _SERIES_NOTES[series]
        return [EdgeFlag(f"series_{series.lower()}", tone, label, detail)]
    return []


def _history_flags(df, info: Dict, today: _dt.date) -> List[EdgeFlag]:
    n = 0 if df is None else len(df)
    listed = info.get("listed_on")
    since_listing = _sessions_between(listed, today) if isinstance(listed, _dt.date) else None
    if since_listing is None and (n == 0 or n >= _SHORT_HISTORY_SESSIONS):
        # Without a listing date, the frame length only proves a NEW listing:
        # a caller may pass 6 months of bars for a decades-old stock.
        return []
    sessions = since_listing if since_listing is not None else n
    if sessions >= _FULL_HISTORY_SESSIONS:
        return []
    when = f"listed {listed:%d %b %Y}, " if isinstance(listed, _dt.date) else ""
    if sessions < _SHORT_HISTORY_SESSIONS:
        return [EdgeFlag("new_listing", "red", "New listing",
                         f"{when}{sessions} sessions of history — RSI/MACD may be warming up and "
                         "the 50/200-day averages and 52-week levels don't exist yet, so the score "
                         "rests on very little data.")]
    return [EdgeFlag("short_history", "amber", "Under a year of history",
                     f"{when}{sessions} sessions — the 200-day average and 52-week levels are "
                     "incomplete.")]


def _stale_flags(df, today: _dt.date) -> List[EdgeFlag]:
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return []
    out: List[EdgeFlag] = []
    last = df.index[-1].date()
    behind = _sessions_between(last, today)
    # today's session may not have a bar yet before the close
    if behind > _STALE_SESSIONS:
        out.append(EdgeFlag("no_recent_bars", "red", "No recent trades",
                            f"Last price is from {last:%d %b %Y}, {behind} sessions ago — the stock "
                            "may be suspended or illiquid; the score describes stale data."))
    if "Volume" in df.columns:
        vol = pd.to_numeric(df["Volume"].tail(5), errors="coerce").fillna(0)
        zero = int((vol == 0).sum())
        if zero >= 3:
            out.append(EdgeFlag("zero_volume", "red", "Barely traded",
                                f"No volume on {zero} of the last 5 sessions — prices may not "
                                "reflect real trades."))
    return out


def _circuit_flags(df, info: Dict) -> List[EdgeFlag]:
    band = info.get("band")
    if df is None or len(df) < 2 or not band:
        return []
    cur, prev = df.iloc[-1], df.iloc[-2]
    try:
        close, high, low = float(cur["Close"]), float(cur["High"]), float(cur["Low"])
        pc = float(prev["Close"])
    except (KeyError, TypeError, ValueError):
        return []
    if pc <= 0:
        return []
    chg = (close / pc - 1) * 100
    if chg >= band - _CIRCUIT_TOL_PCT and close >= high * 0.999:
        locked = high <= low * 1.001
        return [EdgeFlag("upper_circuit", "red" if locked else "amber",
                         f"At upper circuit (+{band:g}% band)" + (" — locked" if locked else ""),
                         "Price is pinned at the day's limit; buy orders may not fill and the "
                         "move can reverse sharply once the limit lifts.")]
    if chg <= -(band - _CIRCUIT_TOL_PCT) and close <= low * 1.001:
        locked = high <= low * 1.001
        return [EdgeFlag("lower_circuit", "red" if locked else "amber",
                         f"At lower circuit (−{band:g}% band)" + (" — locked" if locked else ""),
                         "Price is pinned at the day's lower limit; sell orders may not fill.")]
    if band <= 5:
        return [EdgeFlag("tight_band", "info", f"{band:g}% price band",
                         "NSE has narrowed this stock's daily price limit, usually after unusual "
                         "moves or for surveillance.")]
    return []
