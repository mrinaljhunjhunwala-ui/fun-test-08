"""session_label (weekend / NSE-holiday wording) and ?ticker= normalisation."""
from datetime import date, datetime

import pytest

from dashboard.shared.deeplink import normalise_ticker_param
from dashboard.shared.session_label import session_label, session_possessive


def test_trading_day_is_today():
    assert session_label(date(2026, 10, 6)) == "today"          # Tuesday
    assert session_possessive(date(2026, 10, 6), title=True) == "Today's"


def test_weekend_points_at_friday():
    assert session_label(date(2026, 9, 27)) == "last session (Fri 25 Sep)"   # Sunday
    assert session_label(datetime(2026, 9, 26, 11, 0)) == "last session (Fri 25 Sep)"
    assert session_possessive(date(2026, 9, 27), title=True) == "Last session's (Fri 25 Sep)"


def test_nse_holiday_points_at_previous_session():
    # Fri 2026-10-02 is Gandhi Jayanti -> Thu 01 Oct
    assert session_label(date(2026, 10, 2)) == "last session (Thu 01 Oct)"
    # The weekend right after the holiday skips it too
    assert session_label(date(2026, 10, 4)) == "last session (Thu 01 Oct)"


def test_preopen_on_trading_day_points_at_previous_session():
    # Mon 12 Oct 2026, 08:30 IST: screens still show Fri 09 Oct
    assert session_label(datetime(2026, 10, 12, 8, 30)) == "last session (Fri 09 Oct)"
    assert session_possessive(datetime(2026, 10, 12, 8, 30), title=True) == "Last session's (Fri 09 Oct)"
    # Mon 05 Oct 08:30: previous Fri 02 Oct is a holiday -> Thu 01 Oct
    assert session_label(datetime(2026, 10, 5, 8, 30)) == "last session (Thu 01 Oct)"


@pytest.mark.parametrize("hh,mm", [(9, 15), (15, 0), (20, 0)])
def test_from_open_onward_is_today(hh, mm):
    assert session_label(datetime(2026, 10, 12, hh, mm)) == "today"
    assert session_possessive(datetime(2026, 10, 12, hh, mm)) == "today's"


def test_holiday_and_weekend_datetimes():
    assert session_label(datetime(2026, 10, 2, 10, 0)) == "last session (Thu 01 Oct)"   # holiday
    assert session_label(datetime(2026, 10, 11, 10, 0)) == "last session (Fri 09 Oct)"  # Sunday


def test_bare_date_stays_date_only():
    assert session_label(date(2026, 10, 12)) == "today"


def test_default_uses_ist_now(monkeypatch):
    import dashboard.shared.session_label as sl
    monkeypatch.setattr(sl, "_now_ist", lambda: datetime(2026, 10, 12, 8, 30))
    assert sl.session_label() == "last session (Fri 09 Oct)"
    monkeypatch.setattr(sl, "_now_ist", lambda: datetime(2026, 10, 12, 9, 15))
    assert sl.session_label() == "today"


@pytest.mark.parametrize("raw,exp", [
    ("hdfcbank", "HDFCBANK.NS"), (" INFY.NS ", "INFY.NS"), ("M&M", "M&M.NS"),
    ("BAJAJ-AUTO", "BAJAJ-AUTO.NS"), ("500325.bo", "500325.BO"), (["TCS"], "TCS.NS"),
    ("", None), (None, None), ("A;DROP", None), ("<script>", None), ("X" * 21, None),
    ("INFY.NYSE", None),
])
def test_normalise_ticker_param(raw, exp):
    assert normalise_ticker_param(raw) == exp
