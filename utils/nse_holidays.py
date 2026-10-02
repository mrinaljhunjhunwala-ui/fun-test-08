"""
utils/nse_holidays.py — the NSE equity holiday calendar (single source of truth).

Dependency-free so analysis/ (which may not import dashboard/) can use it —
e.g. analysis.verdict_ledger dating rows by trading session.
dashboard/shared/market_hours.py re-exports NSE_HOLIDAYS for existing callers.
"""
from __future__ import annotations

import logging
from datetime import date

_log = logging.getLogger("utils.nse_holidays")

# ── NSE Holiday Calendar ─────────────────────────────────────────────────────
# Source: NSE India official holiday list.
# ADD new years at the bottom.  Format: "YYYY-MM-DD".
NSE_HOLIDAYS: frozenset[str] = frozenset({
    # ── 2024 ──
    "2024-01-22",   # Ram Mandir Consecration (special closure)
    "2024-01-26",   # Republic Day
    "2024-03-08",   # Mahashivratri
    "2024-03-25",   # Holi
    "2024-03-29",   # Good Friday
    "2024-04-11",   # Id-Ul-Fitr (Ramadan Eid)
    "2024-04-14",   # Dr. Ambedkar Jayanti
    "2024-04-17",   # Ram Navami
    "2024-04-21",   # Mahavir Jayanti
    "2024-05-23",   # Buddha Purnima
    "2024-06-17",   # Bakri Id
    "2024-07-17",   # Muharram
    "2024-08-15",   # Independence Day
    "2024-10-02",   # Mahatma Gandhi Jayanti
    "2024-10-24",   # Dussehra (Vijaya Dashami)
    "2024-11-01",   # Diwali Laxmi Puja
    "2024-11-15",   # Gurunanak Jayanti
    "2024-12-25",   # Christmas

    # ── 2025 ──
    "2025-01-26",   # Republic Day
    "2025-02-26",   # Mahashivratri
    "2025-03-14",   # Holi
    "2025-03-31",   # Id-Ul-Fitr (Eid)
    "2025-04-10",   # Shri Ram Navami
    "2025-04-14",   # Dr. Ambedkar Jayanti
    "2025-04-18",   # Good Friday
    "2025-05-12",   # Buddha Purnima
    "2025-06-07",   # Bakri Eid
    "2025-06-27",   # Muharram
    "2025-08-15",   # Independence Day
    "2025-08-27",   # Ganesh Chaturthi
    "2025-10-02",   # Mahatma Gandhi Jayanti
    "2025-10-02",   # Dussehra (same day — only 1 closure)
    "2025-10-21",   # Diwali Laxmi Puja
    "2025-10-22",   # Diwali Balipratipada
    "2025-11-05",   # Gurunanak Jayanti
    "2025-12-25",   # Christmas

    # ── 2026 ── (FIX MH1: corrected against NSE's published 2026 equity
    # holiday list — 15 full-day closures. Independence Day 2026 falls on a
    # Saturday, already a non-trading day via the weekday check, but kept
    # here for completeness/consistency with is_trading_day()'s direct
    # date-string lookup.)
    "2026-01-15",   # Special closure — Maharashtra municipal elections (audit 2026-10-03:
                    # NSE's bhavcopy for this date re-issues 14-Jan data; no session traded)
    "2026-01-26",   # Republic Day
    "2026-03-03",   # Holi
    "2026-03-26",   # Shri Ram Navami
    "2026-03-31",   # Shri Mahavir Jayanti
    "2026-04-03",   # Good Friday
    "2026-04-14",   # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",   # Maharashtra Day
    "2026-05-28",   # Bakri Id
    "2026-06-26",   # Muharram
    "2026-08-15",   # Independence Day (falls on a Saturday in 2026)
    "2026-09-14",   # Ganesh Chaturthi
    "2026-10-02",   # Mahatma Gandhi Jayanti
    "2026-10-20",   # Dussehra
    "2026-11-10",   # Diwali — Balipratipada
    "2026-11-24",   # Prakash Gurpurb Sri Guru Nanak Dev
    "2026-12-25",   # Christmas

    # ── 2027 ── PROVISIONAL (added 2026-10-03, before NSE's 2027 circular,
    # which usually lands mid-December). Weekday closures only — 2027's
    # Maha Shivaratri, Maharashtra Day, Independence Day, Ganesh Chaturthi,
    # Gandhi Jayanti, Dasara, Guru Nanak Jayanti and Christmas all fall on a
    # Saturday/Sunday. Fixed-date entries are firm; lunar-calendar entries are
    # projections and MUST be re-checked against the NSE circular, then drop
    # 2027 from PROVISIONAL_YEARS below.
    "2027-01-26",   # Republic Day                     (fixed date)
    "2027-03-10",   # Id-ul-Fitr (Ramzan Id)           (lunar — projected)
    "2027-03-22",   # Holi                             (lunar — projected)
    "2027-03-26",   # Good Friday (Easter 28-Mar-2027) (fixed by Easter)
    "2027-04-14",   # Dr. Baba Saheb Ambedkar Jayanti  (fixed date)
    "2027-04-15",   # Ram Navami                       (lunar — projected)
    "2027-04-19",   # Mahavir Jayanti                  (lunar — projected)
    "2027-05-17",   # Bakri Id                         (lunar — projected)
    "2027-06-15",   # Muharram                         (lunar — projected)
    "2027-10-29",   # Diwali — Laxmi Pujan / Balipratipada (lunar — projected)
})

# Years the calendar above covers, and those still awaiting NSE's circular.
# FIX HOLIDAY-YEAR (audit 2026-10-03): the list used to stop at 2026 with no
# signal, so every 2027 holiday would have counted as a trading day.
COVERED_YEARS: frozenset[int] = frozenset({2024, 2025, 2026, 2027})
PROVISIONAL_YEARS: frozenset[int] = frozenset({2027})

_warned_years: set[int] = set()


def calendar_status(year: int) -> str:
    """"official", "provisional", or "missing" for *year*."""
    if year not in COVERED_YEARS:
        return "missing"
    return "provisional" if year in PROVISIONAL_YEARS else "official"


def is_trading_day(d: date) -> bool:
    """True if *d* is a weekday and not an NSE holiday.

    For a year the calendar doesn't cover, holidays can't be known: weekdays
    still return True, and a warning is logged once per year so the gap shows
    up in logs instead of silently mis-dating sessions.
    """
    if d.year not in COVERED_YEARS and d.year not in _warned_years:
        _warned_years.add(d.year)
        _log.warning("NSE holiday calendar has no entries for %d — weekday holidays "
                     "will be treated as trading days. Add the year to "
                     "utils/nse_holidays.py from NSE's circular.", d.year)
    return d.weekday() < 5 and d.isoformat() not in NSE_HOLIDAYS
