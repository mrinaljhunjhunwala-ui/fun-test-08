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


@pytest.mark.parametrize("raw,exp", [
    ("hdfcbank", "HDFCBANK.NS"), (" INFY.NS ", "INFY.NS"), ("M&M", "M&M.NS"),
    ("BAJAJ-AUTO", "BAJAJ-AUTO.NS"), ("500325.bo", "500325.BO"), (["TCS"], "TCS.NS"),
    ("", None), (None, None), ("A;DROP", None), ("<script>", None), ("X" * 21, None),
    ("INFY.NYSE", None),
])
def test_normalise_ticker_param(raw, exp):
    assert normalise_ticker_param(raw) == exp
