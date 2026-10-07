"""Market-session wording for "today" labels.

On a weekend or NSE holiday the quotes, scans and pick lists on screen
describe the LAST trading session, not today. ``session_label()`` returns
"today" on a trading day and e.g. "last session (Fri 03 Oct)" otherwise, so
headers stop claiming a session that never happened. No Streamlit imports.

Time handling: a ``datetime`` (and the default, IST now) is time-aware --
before the 09:15 IST open on a trading day the screens still show the
PREVIOUS session, so the label is "last session (...)"; from 09:15 onward
(including after the close) it is "today". A bare ``date`` keeps the old
date-only behaviour: "today" on any trading day.
"""
from __future__ import annotations

from datetime import date, datetime

from dashboard.shared.market_hours import _MARKET_OPEN, _now_ist, is_trading_day, prev_trading_day


def _session_date(now: date | datetime | None) -> date | None:
    """Date of the session on screen: None means "today", else the last session."""
    if now is None:
        now = _now_ist()
    if isinstance(now, datetime):
        d = now.date()
        if is_trading_day(d) and now.time() >= _MARKET_OPEN:
            return None
        return prev_trading_day(d)
    return None if is_trading_day(now) else prev_trading_day(now)


def _cap(s: str, title: bool) -> str:
    return s[:1].upper() + s[1:] if title else s


def session_label(now: date | datetime | None = None, *, title: bool = False) -> str:
    """'today' on a trading day, else 'last session (Fri 03 Oct)'."""
    d = _session_date(now)
    if d is None:
        return _cap("today", title)
    return _cap(f"last session ({d:%a %d %b})", title)


def session_possessive(now: date | datetime | None = None, *, title: bool = False) -> str:
    """"today's" on a trading day, else "last session's (Fri 03 Oct)"."""
    d = _session_date(now)
    if d is None:
        return _cap("today's", title)
    return _cap(f"last session's ({d:%a %d %b})", title)
