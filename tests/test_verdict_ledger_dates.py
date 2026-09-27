"""
FIX LEDGER-IST: verdict_log.logged_date is the NSE session the entry price
belongs to, not the server's (UTC) wall-clock date; forward-return targets
only count sessions whose close is final.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis import verdict_ledger as vl  # noqa: E402
from analysis.final_verdict import combine  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc


def _ist(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=IST)


# 2026-09-25 is a Friday, 09-28 a Monday.
@pytest.mark.parametrize("now, expected", [
    (_ist(2026, 9, 24, 20, 0),  dt.date(2026, 9, 24)),   # Thu evening scan → Thu
    (_ist(2026, 9, 25, 1, 0),   dt.date(2026, 9, 24)),   # Fri 01:00 IST (Thu in UTC) → Thu
    (_ist(2026, 9, 25, 8, 0),   dt.date(2026, 9, 24)),   # Fri pre-open → Thu's close
    (_ist(2026, 9, 25, 9, 15),  dt.date(2026, 9, 25)),   # Fri at the open → Fri
    (_ist(2026, 9, 25, 14, 0),  dt.date(2026, 9, 25)),   # Fri intraday → Fri
    (_ist(2026, 9, 26, 12, 0),  dt.date(2026, 9, 25)),   # Saturday → Fri
    (_ist(2026, 9, 27, 23, 0),  dt.date(2026, 9, 25)),   # Sunday → Fri
    (_ist(2026, 9, 28, 7, 0),   dt.date(2026, 9, 25)),   # Mon pre-open → Fri
])
def test_session_date(now, expected):
    assert vl.session_date(now) == expected


def test_session_date_accepts_utc_server_clock():
    # 19:30 UTC Thu == 01:00 IST Fri → still Thursday's session.
    assert vl.session_date(dt.datetime(2026, 9, 24, 19, 30, tzinfo=UTC)) == dt.date(2026, 9, 24)
    # 04:00 UTC Fri == 09:30 IST Fri → Friday's session (UTC date alone agrees here).
    assert vl.session_date(dt.datetime(2026, 9, 25, 4, 0, tzinfo=UTC)) == dt.date(2026, 9, 25)


@pytest.mark.parametrize("now, expected", [
    (_ist(2026, 9, 25, 14, 0),  dt.date(2026, 9, 24)),   # Fri intraday: Fri bar not final
    (_ist(2026, 9, 25, 15, 30), dt.date(2026, 9, 25)),   # Fri at the close
    (_ist(2026, 9, 26, 10, 0),  dt.date(2026, 9, 25)),   # Saturday
    (_ist(2026, 9, 28, 9, 30),  dt.date(2026, 9, 25)),   # Mon intraday → Fri
])
def test_last_closed_session(now, expected):
    assert vl.last_closed_session(now) == expected


def test_log_verdict_stamps_session_date(monkeypatch):
    monkeypatch.setattr(vl, "_now_ist", lambda: _ist(2026, 9, 25, 1, 0))
    fv = combine(composite_score=62.0, composite_action="Bullish")
    assert vl.log_verdict(ticker="INFY.NS", final_verdict=fv, entry_price=1500.0,
                          source="tomorrow_watchlist") is not None
    led = vl.load_ledger()
    assert str(led["logged_date"].iloc[0]) == "2026-09-24"


def test_evening_and_next_preopen_scan_dedup_to_one_row(monkeypatch):
    """Both scans see Thursday's close — they're the same observation."""
    fv = combine(composite_score=62.0, composite_action="Bullish")
    for now in (_ist(2026, 9, 24, 20, 0), _ist(2026, 9, 25, 8, 30)):
        monkeypatch.setattr(vl, "_now_ist", lambda now=now: now)
        vl.log_verdict(ticker="INFY.NS", final_verdict=fv, entry_price=1500.0,
                       source="tomorrow_watchlist")
    assert len(vl.load_ledger()) == 1


# ── NSE holidays (utils.nse_holidays) ────────────────────────────────────────
# 2026-09-14 (Mon) is Ganesh Chaturthi; 2026-10-02 (Fri) is Gandhi Jayanti.

@pytest.mark.parametrize("now, expected", [
    (_ist(2026, 9, 14, 11, 0),  dt.date(2026, 9, 11)),   # holiday Mon → Fri before
    (_ist(2026, 9, 15, 8, 0),   dt.date(2026, 9, 11)),   # Tue pre-open → skips holiday
    (_ist(2026, 9, 15, 10, 0),  dt.date(2026, 9, 15)),   # Tue after open → Tue
    (_ist(2026, 10, 3, 10, 0),  dt.date(2026, 10, 1)),   # Sat after Fri holiday → Thu
])
def test_session_date_skips_holidays(now, expected):
    assert vl.session_date(now) == expected


def test_last_closed_session_skips_holidays():
    assert vl.last_closed_session(_ist(2026, 9, 15, 11, 0)) == dt.date(2026, 9, 11)


def test_horizon_counts_sessions_not_holidays():
    # Fri 09-11 + 1 session → Tue 09-15 (Mon is a holiday); plain BDay said Mon.
    assert vl.horizon_target_date(dt.date(2026, 9, 11), 1) == dt.date(2026, 9, 15)
    # 5 sessions from Thu 09-10: Fri, Tue, Wed, Thu, Fri → 09-18.
    assert vl.horizon_target_date(dt.date(2026, 9, 10), 5) == dt.date(2026, 9, 18)
    # No holiday in the window → unchanged from weekday counting.
    assert vl.horizon_target_date(dt.date(2026, 9, 21), 5) == dt.date(2026, 9, 28)


def test_market_hours_reexports_the_same_calendar():
    from dashboard.shared.market_hours import NSE_HOLIDAYS as dash
    from utils.nse_holidays import NSE_HOLIDAYS as pure
    assert dash is pure
    assert "2026-09-14" in pure
