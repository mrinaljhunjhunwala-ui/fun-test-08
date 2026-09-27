"""
dashboard/shared/market_hours.py
──────────────────────────────────────────────────────────────────────────────
NSE market-hours, holiday calendar, and session utilities.

Usage
─────
    from dashboard.shared.market_hours import (
        is_market_open,
        is_trading_day,
        market_status,          # rich dict for UI
        next_open_datetime,
        cache_ttl_seconds,      # smart TTL: short when open, long when closed
        NSE_HOLIDAYS,
    )

All times are evaluated in IST (Asia/Kolkata, UTC+5:30).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Literal

# ── IST timezone (stdlib zoneinfo; falls back to manual offset) ──────────────
try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except ImportError:                          # Python < 3.9 without backport
    from datetime import timezone
    _IST = timezone(timedelta(hours=5, minutes=30))

log = logging.getLogger(__name__)

# ── NSE trading session times (IST) ─────────────────────────────────────────
_MARKET_OPEN   = time(9, 15)
_MARKET_CLOSE  = time(15, 30)
_PREOPEN_START = time(9,  0)
_PREOPEN_END   = time(9, 15)
_CLOSING_START = time(15, 30)   # closing session / call auction
_CLOSING_END   = time(15, 40)
_SQUAREOFF_CUT = time(15, 20)   # intraday auto square-off deadline (conservative)

# ── NSE Holiday Calendar ── moved to utils/nse_holidays.py (pure, importable
# from analysis/). Re-exported here so existing importers keep working.
from utils.nse_holidays import NSE_HOLIDAYS  # noqa: E402,F401
from utils.nse_holidays import is_trading_day as _pure_is_trading_day  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Core helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    """Current datetime in IST."""
    return datetime.now(_IST)


def is_trading_day(d: date | None = None) -> bool:
    """
    Return True if *d* (default: today IST) is a weekday and not an NSE holiday.
    """
    if d is None:
        d = _now_ist().date()
    return _pure_is_trading_day(d)


def is_market_open(dt: datetime | None = None) -> bool:
    """
    Return True if the NSE cash market is currently open for trading.
    Pre-open (9:00–9:14) and post-close sessions are treated as CLOSED
    for the purposes of live-price freshness and trade execution guards.
    """
    if dt is None:
        dt = _now_ist()
    if not is_trading_day(dt.date()):
        return False
    t = dt.time()
    return _MARKET_OPEN <= t < _MARKET_CLOSE


def is_preopen(dt: datetime | None = None) -> bool:
    """Return True during the NSE pre-open call auction (9:00–9:14 IST)."""
    if dt is None:
        dt = _now_ist()
    if not is_trading_day(dt.date()):
        return False
    return _PREOPEN_START <= dt.time() < _PREOPEN_END


def is_squareoff_window(dt: datetime | None = None) -> bool:
    """
    Return True if we are within the intraday auto square-off window:
    3:20 PM → 3:30 PM IST on a trading day.
    Brokers typically force-close MIS positions between 3:20–3:25.
    """
    if dt is None:
        dt = _now_ist()
    if not is_trading_day(dt.date()):
        return False
    return _SQUAREOFF_CUT <= dt.time() < _MARKET_CLOSE


SessionType = Literal["open", "preopen", "closing", "closed", "holiday", "weekend"]


def market_session(dt: datetime | None = None) -> SessionType:
    """Classify the current moment into a named session."""
    if dt is None:
        dt = _now_ist()
    d = dt.date()
    t = dt.time()
    if d.weekday() >= 5:
        return "weekend"
    if d.isoformat() in NSE_HOLIDAYS:
        return "holiday"
    if _PREOPEN_START <= t < _PREOPEN_END:
        return "preopen"
    if _MARKET_OPEN <= t < _MARKET_CLOSE:
        return "open"
    if _MARKET_CLOSE <= t < _CLOSING_END:
        return "closing"
    return "closed"


def market_status(dt: datetime | None = None) -> dict:
    """
    Return a rich status dict suitable for rendering in the UI.

    Keys
    ────
        is_open         bool
        session         SessionType
        label           str   e.g. "🟢 Market Open"
        sublabel        str   e.g. "Closes in 2 h 14 m"
        color           str   hex colour for badge
        next_event      str   human-readable description of next state change
        next_event_dt   datetime | None
    """
    if dt is None:
        dt = _now_ist()

    session = market_session(dt)
    open_flag = session == "open"

    _LABELS = {
        "open":     ("🟢 Market Open",     "#26a69a"),
        "preopen":  ("🟡 Pre-Open",         "#ff9500"),
        "closing":  ("🟠 Closing Session",  "#ff6b35"),
        "closed":   ("🔴 Market Closed",    "#ef5350"),
        "holiday":  ("🎉 Market Holiday",   "#9b59b6"),
        "weekend":  ("😴 Weekend",          "#607d8b"),
    }
    label, color = _LABELS.get(session, ("❓ Unknown", "#aaa"))

    # ── next event ──────────────────────────────────────────────────────────
    nxt_dt = next_open_datetime(dt)
    nxt_close = _next_close_datetime(dt)

    if session == "open":
        sublabel = f"Closes in {_fmt_delta(nxt_close - dt)}"
        next_event = f"Market closes at 3:30 PM IST"
        next_event_dt = nxt_close
    elif session == "preopen":
        opens_in = datetime.combine(dt.date(), _MARKET_OPEN, tzinfo=_IST) - dt
        sublabel = f"Opens in {_fmt_delta(opens_in)}"
        next_event = "Regular session opens at 9:15 AM IST"
        next_event_dt = datetime.combine(dt.date(), _MARKET_OPEN, tzinfo=_IST)
    else:
        sublabel = f"Opens {_fmt_delta(nxt_dt - dt, future=True)}"
        next_event = f"Next open: {nxt_dt.strftime('%a %d %b, 9:15 AM IST')}"
        next_event_dt = nxt_dt

    return {
        "is_open":       open_flag,
        "session":       session,
        "label":         label,
        "sublabel":      sublabel,
        "color":         color,
        "next_event":    next_event,
        "next_event_dt": next_event_dt,
        "ist_now":       dt,
    }


def next_open_datetime(from_dt: datetime | None = None) -> datetime:
    """
    Return the datetime of the next NSE market open (9:15 AM IST)
    on or after *from_dt*.  Skips weekends and holidays.
    """
    if from_dt is None:
        from_dt = _now_ist()
    d = from_dt.date()
    t = from_dt.time()

    # If today is a trading day AND we haven't passed 9:15 yet → today
    if is_trading_day(d) and t < _MARKET_OPEN:
        return datetime.combine(d, _MARKET_OPEN, tzinfo=_IST)

    # Otherwise walk forward
    d += timedelta(days=1)
    for _ in range(30):          # safety: won't have >30 consecutive non-trading days
        if is_trading_day(d):
            return datetime.combine(d, _MARKET_OPEN, tzinfo=_IST)
        d += timedelta(days=1)
    raise RuntimeError("Could not find next trading day within 30 days — check holiday list")


def _next_close_datetime(from_dt: datetime) -> datetime:
    """Return today's close datetime if market is open, else next close."""
    d = from_dt.date()
    if is_trading_day(d) and from_dt.time() < _MARKET_CLOSE:
        return datetime.combine(d, _MARKET_CLOSE, tzinfo=_IST)
    nxt = next_open_datetime(from_dt)
    return datetime.combine(nxt.date(), _MARKET_CLOSE, tzinfo=_IST)


def cache_ttl_seconds(
    open_ttl: int = 60,
    closed_ttl: int = 3600,
    dt: datetime | None = None,
) -> int:
    """
    Return a smart cache TTL:
    - *open_ttl*   (default 60 s)   when market is open
    - *closed_ttl* (default 3600 s) when market is closed / holiday / weekend

    Use as the `ttl` argument to ``@st.cache_data``.
    """
    return open_ttl if is_market_open(dt) else closed_ttl


def _fmt_delta(delta: timedelta, future: bool = False) -> str:
    """Format a timedelta as a human-readable string like '2 h 14 m'."""
    total = max(int(delta.total_seconds()), 0)
    h, rem = divmod(total, 3600)
    m = rem // 60
    if h > 0:
        return f"{h} h {m} m"
    if m > 0:
        return f"{m} m"
    return "< 1 m"


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: trading-day aware date range
# ─────────────────────────────────────────────────────────────────────────────

def prev_trading_day(d: date | None = None) -> date:
    """Return the most recent trading day before *d* (default: today IST)."""
    if d is None:
        d = _now_ist().date()
    d -= timedelta(days=1)
    for _ in range(14):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    raise RuntimeError("Cannot find previous trading day — check holiday list")


@lru_cache(maxsize=1)
def _today_ist_str() -> str:
    """Cached today string (safe to use within a single process run)."""
    return _now_ist().date().isoformat()
