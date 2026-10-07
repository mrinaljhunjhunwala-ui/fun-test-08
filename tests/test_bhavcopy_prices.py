"""Offline tests for the NSE bhavcopy daily-close fallback (fetch_single Tier 3)."""
import datetime as dt
import logging

import pandas as pd
import pytest

import data.fetcher as fetcher
import data.nse_bhavcopy_prices as bp
from utils.nse_holidays import is_trading_day

_HDR = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, "
        "LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, "
        "NO_OF_TRADES, DELIV_QTY, DELIV_PER")
TODAY = dt.date(2026, 10, 7)


def _csv(d: dt.date, rows):
    lines = [_HDR]
    for sym, series, close in rows:
        lines.append(f"{sym}, {series}, {d.strftime('%d-%b-%Y')}, {close}, {close - 1}, "
                     f"{close + 2}, {close - 2}, {close}, {close}, {close}, 1000, 1, 10, 500, 50.00")
    return "\n".join(lines) + "\n"


def _sessions(n):
    out, d = [], TODAY
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


def _downloader(price_for, calls=None):
    def _dl(d):
        if calls is not None:
            calls.append(d)
        p = price_for(d)
        return _csv(d, [("RELIANCE", "EQ", p), ("RELIANCE", "BL", 1.0),
                        ("SMALLCO", "BE", 50.0), ("NIFTYX", "N1", 9.0)])
    return _dl


def test_parse_keeps_eq_and_be_only():
    d = dt.date(2026, 10, 6)
    df = bp.parse_ohlcv(_csv(d, [("A", "EQ", 10.0), ("B", "BE", 20.0), ("C", "N1", 30.0)]))
    assert set(df["SYMBOL"]) == {"A", "B"}
    row = df[df["SYMBOL"] == "A"].iloc[0]
    assert (row["Open"], row["High"], row["Low"], row["Close"], row["Volume"]) == (9, 12, 8, 10, 1000)


def test_parse_schema_drift_raises():
    with pytest.raises(ValueError, match="schema drift"):
        bp.parse_ohlcv("SYMBOL,SERIES,DATE1\nA,EQ,06-Oct-2026\n")


def test_history_shape_holidays_and_cache(tmp_path):
    calls = []
    dl = _downloader(lambda d: 100.0 + d.toordinal() % 7, calls)
    df = bp.fetch_bhavcopy_history("RELIANCE.NS", period="1mo", download=dl,
                                   today=TODAY, cache_dir=str(tmp_path))
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.name == "Date" and df.index.is_monotonic_increasing
    assert len(df) == 21
    # Holiday / weekend gap: no non-trading date is requested or served.
    assert all(is_trading_day(d) for d in calls)
    assert all(is_trading_day(ts.date()) for ts in df.index)
    assert dt.date(2026, 10, 2) not in calls   # Gandhi Jayanti
    # Second call reads the on-disk cache only.
    calls.clear()
    df2 = bp.fetch_bhavcopy_history("RELIANCE", period="1mo", download=dl,
                                    today=TODAY, cache_dir=str(tmp_path))
    assert calls == []
    pd.testing.assert_frame_equal(df, df2)


def test_be_series_symbol_served(tmp_path):
    df = bp.fetch_bhavcopy_history("SMALLCO.NS", period="5d", download=_downloader(lambda d: 1.0),
                                   today=TODAY, cache_dir=str(tmp_path))
    assert len(df) == 5 and (df["Close"] == 50.0).all()


def test_long_period_capped_with_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="data.nse_bhavcopy_prices"):
        df = bp.fetch_bhavcopy_history("RELIANCE.NS", period="2y",
                                       download=_downloader(lambda d: 100.0),
                                       today=TODAY, cache_dir=str(tmp_path))
    assert len(df) == bp.MAX_SESSIONS
    assert "history truncated" in caplog.text


def test_split_like_jump_truncates(tmp_path, caplog):
    sess = _sessions(10)
    split_day = sess[6]
    dl = _downloader(lambda d: 1000.0 if d < split_day else 200.0)
    with caplog.at_level(logging.WARNING, logger="data.nse_bhavcopy_prices"):
        df = bp.fetch_bhavcopy_history("RELIANCE.NS", period="5d", download=dl,
                                       today=TODAY, cache_dir=str(tmp_path))
    assert (df["Close"] == 200.0).all()
    with caplog.at_level(logging.WARNING, logger="data.nse_bhavcopy_prices"):
        df = bp.fetch_bhavcopy_history("RELIANCE.NS", period="1mo", download=dl,
                                       today=TODAY, cache_dir=str(tmp_path))
    assert df.index[0].date() == split_day and (df["Close"] == 200.0).all()
    assert "unadjusted corporate action" in caplog.text


def test_unknown_symbol_raises(tmp_path):
    with pytest.raises(ValueError, match="no rows"):
        bp.fetch_bhavcopy_history("NOPE.NS", period="5d", download=_downloader(lambda d: 1.0),
                                  today=TODAY, cache_dir=str(tmp_path))


def test_fetch_single_falls_through_to_bhavcopy(monkeypatch):
    class _Angel:
        @staticmethod
        def is_configured():
            return True

        @staticmethod
        def fetch_historical(*a, **k):
            raise RuntimeError("angel down")

    def _boom(*a, **k):
        raise ValueError("down")

    monkeypatch.setattr(fetcher, "_FETCH_CACHE", {}, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "data.angel_fetcher", _Angel)
    monkeypatch.setattr(fetcher, "_stooq_enabled", lambda: True)
    monkeypatch.setattr(fetcher, "_stooq_breaker_is_tripped", lambda: False)
    monkeypatch.setattr(fetcher, "_stooq_breaker_record", lambda success: None)
    monkeypatch.setattr(fetcher, "_fetch_stooq", _boom)
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct", _boom)
    monkeypatch.setattr(bp, "_default_download", _downloader(lambda d: 123.0))

    real = bp.fetch_bhavcopy_history
    monkeypatch.setattr(bp, "fetch_bhavcopy_history",
                        lambda t, period="2y": real(t, period=period, today=TODAY))
    df = fetcher.fetch_single("RELIANCE.NS", period="5d", interval="1d")
    assert len(df) == 5 and (df["Close"] == 123.0).all()
    assert fetcher.get_last_diagnostic("NSE-bhavcopy")["ok"] is True


def test_fetch_single_bhavcopy_not_used_for_intraday(monkeypatch):
    monkeypatch.setattr(fetcher, "_FETCH_CACHE", {}, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "data.angel_fetcher",
                        type("X", (), {"is_configured": lambda: False}))
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("down")))
    called = []
    monkeypatch.setattr(bp, "fetch_bhavcopy_history", lambda *a, **k: called.append(1))
    with pytest.raises(ValueError):
        fetcher.fetch_single("RELIANCE.NS", period="5d", interval="15m")
    assert called == []


# ── Review fixes (PR #305) ───────────────────────────────────────────────────

_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def test_last_published_session_skips_today_before_evening():
    # 2026-10-07 is a Wednesday trading day.
    assert bp.last_published_session(dt.datetime(2026, 10, 7, 12, 0, tzinfo=_IST)) == dt.date(2026, 10, 6)
    assert bp.last_published_session(dt.datetime(2026, 10, 7, 19, 0, tzinfo=_IST)) == dt.date(2026, 10, 7)
    # Monday morning -> previous Thursday (Fri 2026-10-02 is Gandhi Jayanti).
    got = bp.last_published_session(dt.datetime(2026, 10, 5, 9, 0, tzinfo=_IST))
    assert got == dt.date(2026, 10, 1)


def test_negative_cache_stops_rerequests(tmp_path):
    calls = []
    good = _downloader(lambda d: 100.0)
    missing = _sessions(1)[0]

    def dl(d):
        calls.append(d)
        if d == missing:
            raise ValueError("404")
        return good(d)
    for _ in range(3):
        bp.fetch_bhavcopy_history("RELIANCE.NS", period="5d", download=dl,
                                  today=TODAY, cache_dir=str(tmp_path))
    assert calls.count(missing) == 1


def test_bonus_like_drop_truncates_only_when_nifty_calm(tmp_path):
    sess = _sessions(25)
    ex = sess[-6]
    dl = _downloader(lambda d: 300.0 if d < ex else 200.0)        # -33%, 1:2 bonus
    calm = pd.Series(0.01, index=pd.DatetimeIndex([pd.Timestamp(d) for d in sess]))
    df = bp.fetch_bhavcopy_history("RELIANCE.NS", period="1mo", download=dl, today=TODAY,
                                   cache_dir=str(tmp_path), nifty=calm)
    assert df.index[0].date() == ex
    crash = calm.copy()
    crash[pd.Timestamp(ex)] = 0.08
    df = bp.fetch_bhavcopy_history("RELIANCE.NS", period="1mo", download=dl, today=TODAY,
                                   cache_dir=str(tmp_path), nifty=crash)
    assert len(df) == 21


def _bhav_frame(tmp_path):
    return bp.fetch_bhavcopy_history("RELIANCE.NS", period="2y", download=_downloader(lambda d: 100.0),
                                     today=TODAY, cache_dir=str(tmp_path))


def test_frame_attrs_tagged(tmp_path):
    df = _bhav_frame(tmp_path)
    assert df.attrs == {"source": "NSE-bhavcopy", "unadjusted": True,
                        "truncated": True, "sessions": bp.MAX_SESSIONS}


def test_degraded_source_flag_and_list_exclusion(tmp_path):
    from analysis.edge_flags import edge_flags, list_exclusion
    df = _bhav_frame(tmp_path)
    flags = edge_flags(df, {}, today=TODAY)
    f = next(f for f in flags if f.key == "degraded_source")
    assert f.tone == "red" and f.label == "Degraded price data" and "60 sessions" in f.detail
    assert list_exclusion(df, {}, today=TODAY) == "degraded price data (backup source)"
    plain = df.copy()
    plain.attrs = {}
    assert all(f.key != "degraded_source" for f in edge_flags(plain, {}, today=TODAY))


def test_attrs_survive_fetch_cache_and_indicators(monkeypatch):
    from utils.indicators import add_all_indicators
    monkeypatch.setattr(fetcher, "_FETCH_CACHE", {}, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "data.angel_fetcher",
                        type("X", (), {"is_configured": lambda: False}))
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("down")))
    monkeypatch.setattr(bp, "_default_download", _downloader(lambda d: 100.0))
    real = bp.fetch_bhavcopy_history
    monkeypatch.setattr(bp, "fetch_bhavcopy_history",
                        lambda t, period="2y": real(t, period=period, today=TODAY))
    first = fetcher.fetch_single("RELIANCE.NS", period="2y")
    again = fetcher.fetch_single("RELIANCE.NS", period="2y")          # cache hit (.copy())
    peek = fetcher.peek_cached("RELIANCE.NS", period="2y")
    for d in (first, again, peek, add_all_indicators(again)):
        assert d.attrs.get("source") == "NSE-bhavcopy"
