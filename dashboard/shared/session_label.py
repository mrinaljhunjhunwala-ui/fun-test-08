"""Market-session wording for "today" labels.

On a weekend or NSE holiday the quotes, scans and pick lists on screen
describe the LAST trading session, not today. ``session_label()`` returns
"today" on a trading day and e.g. "last session (Fri 03 Oct)" otherwise, so
headers stop claiming a session that never happened. No Streamlit imports.
"""
from __future__ import annotations

from datetime import date, datetime

from dashboard.shared.market_hours import _now_ist, is_trading_day, prev_trading_day


def _as_date(now: date | datetime | None) -> date:
    if now is None:
        return _now_ist().date()
    return now.date() if isinstance(now, datetime) else now


def _cap(s: str, title: bool) -> str:
    return s[:1].upper() + s[1:] if title else s


def session_label(now: date | datetime | None = None, *, title: bool = False) -> str:
    """'today' on a trading day, else 'last session (Fri 03 Oct)'."""
    d = _as_date(now)
    if is_trading_day(d):
        return _cap("today", title)
    return _cap(f"last session ({prev_trading_day(d):%a %d %b})", title)


def session_possessive(now: date | datetime | None = None, *, title: bool = False) -> str:
    """"today's" on a trading day, else "last session's (Fri 03 Oct)"."""
    d = _as_date(now)
    if is_trading_day(d):
        return _cap("today's", title)
    return _cap(f"last session's ({prev_trading_day(d):%a %d %b})", title)
