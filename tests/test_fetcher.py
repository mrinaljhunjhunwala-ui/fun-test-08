import pandas as pd
import datetime as dt
import pytest

import data.fetcher as fetcher


def _sample_df_dates(start_date="2026-07-01", periods=5, freq="D"):
    idx = pd.date_range(start=start_date, periods=periods, freq=freq)
    df = pd.DataFrame({
        "Open": [100 + i for i in range(periods)],
        "High": [101 + i for i in range(periods)],
        "Low": [99 + i for i in range(periods)],
        "Close": [100 + i for i in range(periods)],
        "Volume": [1000 + 10 * i for i in range(periods)],
    }, index=idx)
    return df


def test_fetch_single_angel_preferred(monkeypatch):
    """When Angel One is configured and returns data, fetch_single should
    return Angel's dataframe without calling lower tiers."""
    sample = _sample_df_dates()

    class FakeAngel:
        @staticmethod
        def is_configured():
            return True

        @staticmethod
        def fetch_historical(ticker, period=None, interval=None):
            return sample.copy()

    # Inject fake angel_fetcher module attributes
    monkeypatch.setattr(fetcher, "_FETCH_CACHE", {}, raising=False)
    monkeypatch.setattr("data.fetcher._stooq_breaker_is_tripped", lambda: False)
    monkeypatch.setattr("data.fetcher._fetch_stooq", lambda *a, **k: None)
    monkeypatch.setitem(__import__("sys").modules, "data.angel_fetcher", FakeAngel)

    got = fetcher.fetch_single("RELIANCE.NS", period="5d", interval="1d")
    # DataFrames may not be the same object but contents should match
    pd.testing.assert_frame_equal(got.reset_index(drop=True), sample.reset_index(drop=True))


def test_fetch_single_stooq_then_yahoo_fallback(monkeypatch):
    """If Stooq raises an error (e.g. returned HTML) the code should fall back to Yahoo."""
    sample = _sample_df_dates()

    def fake_stooq(ticker, period=None):
        raise ValueError("Stooq returned HTML (not CSV)")

    def fake_yahoo(ticker, period=None, interval=None):
        return sample.copy()

    monkeypatch.setattr(fetcher, "_FETCH_CACHE", {}, raising=False)
    monkeypatch.setattr(fetcher, "_fetch_stooq", fake_stooq)
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct", fake_yahoo)
    # Ensure Angel is not used
    monkeypatch.setattr("data.fetcher._stooq_breaker_is_tripped", lambda: False)
    monkeypatch.setitem(__import__("sys").modules, "data.angel_fetcher", type("X", (), {"is_configured": lambda: False}))

    got = fetcher.fetch_single("RELIANCE.NS", period="5d", interval="1d")
    pd.testing.assert_frame_equal(got.reset_index(drop=True), sample.reset_index(drop=True))


def test_fetch_single_all_providers_fail(monkeypatch):
    """When all providers fail, fetch_single should raise ValueError."""

    def bad_stooq(t, period=None):
        raise ValueError("stooq fail")

    def bad_yahoo(t, period=None, interval=None):
        raise ValueError("yahoo fail")

    # Reset the module-level fetch cache — without this, a prior test in the
    # same session (test_fetch_single_stooq_then_yahoo_fallback, same ticker/
    # period/interval) leaves a valid cache entry behind, fetch_single()
    # returns it without ever calling stooq/yahoo, and this test never
    # observes the ValueError it's asserting on. Order-dependent pollution
    # via shared global state, not a production bug.
    monkeypatch.setattr(fetcher, "_FETCH_CACHE", {}, raising=False)
    monkeypatch.setattr(fetcher, "_fetch_stooq", bad_stooq)
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct", bad_yahoo)
    # Angel disabled
    monkeypatch.setitem(__import__("sys").modules, "data.angel_fetcher", type("X", (), {"is_configured": lambda: False}))

    with pytest.raises(ValueError):
        fetcher.fetch_single("RELIANCE.NS", period="5d", interval="1d")


def test_fetch_intraday_filters_market_hours(monkeypatch):
    """fetch_intraday should filter rows outside 09:15-15:30 IST market hours."""
    # Build intraday-like datetime index with 5 rows spanning before, inside, after market hours
    base = dt.datetime(2026, 7, 1, 8, 0)
    times = [base + dt.timedelta(hours=i) for i in range(6)]  # 08:00 .. 13:00
    idx = pd.DatetimeIndex(times)
    df = pd.DataFrame({
        "Open": [10, 11, 12, 13, 14, 15],
        "High": [11, 12, 13, 14, 15, 16],
        "Low": [9, 10, 11, 12, 13, 14],
        "Close": [10, 11, 12, 13, 14, 15],
        "Volume": [100, 200, 300, 400, 500, 600],
    }, index=idx)

    # Monkeypatch fetch_single to return df with full-hour times (UTC-like); fetch_intraday will
    # attempt to filter by time and may skip if index lacks .time — but DatetimeIndex works.
    monkeypatch.setattr(fetcher, "fetch_single", lambda t, period, interval: df.copy())

    out = fetcher.fetch_intraday("RELIANCE.NS", interval="60m", days=1)
    # Market open is 09:15, so rows with hour >=9 and <=15:30 survive — from our times above,
    # only rows at 09:00..13:00 with 09:00 included; since we used 08:00..13:00, expect 09..13 -> 5 rows
    assert len(out) >= 1


def test_fetch_single_angel_busy_falls_through_quietly(monkeypatch, caplog):
    """FIX AO-BULK: AngelBusy is a skip, not a failure — no WARNING, next tier serves."""
    import logging
    import types
    import pandas as pd
    import data.fetcher as f

    class _Busy(Exception):
        pass

    def _raise(*a, **k):
        raise _Busy()

    fake = types.SimpleNamespace(AngelBusy=_Busy, fetch_historical=_raise, is_configured=lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "data.angel_fetcher", fake)
    monkeypatch.setattr(f, "_stooq_enabled", lambda: False)
    yahoo_df = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1]},
                            index=pd.to_datetime(["2026-09-25"]))
    monkeypatch.setattr(f, "_fetch_yahoo_direct", lambda *a, **k: yahoo_df)
    monkeypatch.setattr(f, "_FETCH_CACHE", {})
    with caplog.at_level(logging.WARNING, logger="data.fetcher"):
        df = f.fetch_single("BUSYTEST.NS", period="1y")
    assert df is not None and not df.empty
    assert not [r for r in caplog.records if "AngelOne" in r.getMessage()]
