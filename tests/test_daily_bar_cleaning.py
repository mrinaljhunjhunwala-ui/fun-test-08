"""Holiday / placeholder bar cleaning in data.fetcher (audit #29)."""
import pandas as pd

from data.fetcher import clean_daily_bars


def _frame(rows):
    idx = pd.DatetimeIndex([r[0] for r in rows], name="Date")
    return pd.DataFrame([r[1:] for r in rows], index=idx,
                        columns=["Open", "High", "Low", "Close", "Volume"])


def test_drops_holiday_and_weekend_bars():
    df = _frame([
        ("2025-03-13", 100, 102, 99, 101, 1000),
        ("2025-03-14", 101, 101, 101, 101, 0),    # Holi
        ("2025-03-15", 101, 101, 101, 101, 0),    # Saturday
        ("2025-03-17", 101, 103, 100, 102, 900),
    ])
    out = clean_daily_bars(df)
    assert [d.date().isoformat() for d in out.index] == ["2025-03-13", "2025-03-17"]
    assert out.attrs["missing_sessions"] == []


def test_keeps_special_saturday_session():
    df = _frame([
        ("2025-01-31", 100, 102, 99, 101, 1000),
        ("2025-02-01", 101, 104, 100, 103, 1500),  # Budget-day Saturday
        ("2025-02-03", 103, 105, 102, 104, 900),
    ])
    assert len(clean_daily_bars(df)) == 3


def test_drops_mid_series_placeholder_and_flags_missing_session():
    df = _frame([
        ("2025-03-17", 100, 102, 99, 101, 1000),
        ("2025-03-18", 101, 101, 101, 101, 0),    # lost session -> flat 0-vol
        ("2025-03-19", 101, 103, 100, 102, 900),
    ])
    out = clean_daily_bars(df)
    assert "2025-03-18" not in [d.date().isoformat() for d in out.index]
    assert out.attrs["missing_sessions"] == ["2025-03-18"]


def test_trailing_suspension_run_is_kept():
    rows = [("2025-03-17", 100, 102, 99, 101, 1000)]
    for d in ["2025-03-18", "2025-03-19", "2025-03-20", "2025-03-21", "2025-03-24"]:
        rows.append((d, 101, 101, 101, 101, 0))
    out = clean_daily_bars(_frame(rows))
    assert len(out) == 6
    assert (out["Volume"].tail(5) == 0).all()


def test_short_trailing_placeholder_is_dropped():
    df = _frame([
        ("2025-03-17", 100, 102, 99, 101, 1000),
        ("2025-03-18", 101, 101, 101, 101, 0),
    ])
    assert len(clean_daily_bars(df)) == 1
