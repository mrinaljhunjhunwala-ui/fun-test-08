"""Holiday-calendar coverage (audit 2026-10-03).

The calendar used to end at 2026 with no signal, so 26-Jan-2027 counted as a
trading day for alerts, cache TTLs, prev_trading_day and the verdict ledger.
"""
import logging
from datetime import date

import pytest

from utils import nse_holidays as H


@pytest.mark.parametrize("d", [
    date(2026, 1, 15),   # Maharashtra municipal-election closure
    date(2027, 1, 26),   # Republic Day
    date(2027, 3, 26),   # Good Friday
    date(2027, 10, 29),  # Diwali
])
def test_known_closures_are_not_trading_days(d):
    assert d.weekday() < 5
    assert H.is_trading_day(d) is False


def test_every_listed_holiday_parses_and_is_in_a_covered_year():
    for s in H.NSE_HOLIDAYS:
        assert date.fromisoformat(s).year in H.COVERED_YEARS, s


def test_2027_only_lists_weekday_closures():
    for s in H.NSE_HOLIDAYS:
        d = date.fromisoformat(s)
        if d.year == 2027:
            assert d.weekday() < 5, s


def test_calendar_status():
    assert H.calendar_status(2026) == "official"
    assert H.calendar_status(2027) == "provisional"
    assert H.calendar_status(2031) == "missing"


def test_uncovered_year_warns_once(caplog):
    H._warned_years.discard(2031)
    with caplog.at_level(logging.WARNING, logger="utils.nse_holidays"):
        assert H.is_trading_day(date(2031, 1, 27)) is True
        H.is_trading_day(date(2031, 1, 28))
    assert sum("2031" in r.getMessage() for r in caplog.records) == 1


def test_current_year_is_covered():
    # Fails on 1 January if nobody added the new year — a loud reminder
    # instead of a silent year of wrong trading days.
    assert H.calendar_status(date.today().year) != "missing"


# ── 2024/2025 corrections (audit 2026-10-05: NSE circulars + RELIANCE volume) ──
@pytest.mark.parametrize("d", ["2024-05-01", "2024-05-20", "2024-11-20", "2025-05-01"])
def test_missing_2024_25_closures_added(d):
    assert H.is_trading_day(date.fromisoformat(d)) is False


@pytest.mark.parametrize("d", ["2024-05-23", "2024-10-24", "2025-05-12", "2025-06-27"])
def test_wrongly_listed_trading_days_removed(d):
    # Full sessions (RELIANCE traded 10-22M shares on each).
    assert H.is_trading_day(date.fromisoformat(d)) is True
