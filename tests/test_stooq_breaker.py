"""
FIX SPEED2 regression tests — Stooq circuit breaker in data/fetcher.py.

Without this breaker, a fully-degraded Stooq (geo-block/rate-limit/
maintenance — all observed in production) makes every ticker in a batch
fetch pay a ~4s Stooq timeout before falling through to Yahoo, which is
what blew the Warm Top Picks GitHub Actions job's 8-minute time budget on
a ~1,400+ ticker universe scan.

Run:  py -m pytest tests/test_stooq_breaker.py -q
"""
import logging
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _good_df():
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    return pd.DataFrame({"Open": [1, 2, 3], "High": [1, 2, 3], "Low": [1, 2, 3],
                         "Close": [1, 2, 3], "Volume": [10, 20, 30]}, index=idx)


def _boom(*a, **k):
    raise ConnectionError("simulated Stooq outage")


@pytest.fixture(autouse=True)
def _isolate_breaker(request, monkeypatch):
    """Every test in this file starts and ends with a fresh, untripped breaker.
    Stooq is off by default (FIX STOOQ-OFF); the breaker tests enable it."""
    from data import fetcher
    if "stooq_off" not in request.keywords:
        monkeypatch.setenv("NSE_ENABLE_STOOQ", "1")
    else:
        monkeypatch.delenv("NSE_ENABLE_STOOQ", raising=False)
    fetcher._reset_stooq_breaker()
    fetcher._FETCH_CACHE.clear()
    yield
    fetcher._reset_stooq_breaker()
    fetcher._FETCH_CACHE.clear()


def test_breaker_untripped_before_threshold(monkeypatch):
    """Fewer than THRESHOLD consecutive failures — Stooq keeps getting tried."""
    from data import fetcher, angel_fetcher
    monkeypatch.setattr(angel_fetcher, "is_configured", lambda: False)
    monkeypatch.setattr(fetcher, "_fetch_stooq", _boom)
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct",
                        lambda t, period, interval: _good_df())

    for i in range(fetcher._STOOQ_BREAKER_THRESHOLD - 1):
        fetcher._FETCH_CACHE.clear()
        fetcher.fetch_single(f"FAKE{i}.NS", period="1y")

    assert not fetcher._stooq_breaker_is_tripped()


def test_breaker_trips_after_threshold_consecutive_failures(monkeypatch, caplog):
    """THRESHOLD consecutive Stooq failures trips the breaker."""
    from data import fetcher, angel_fetcher
    monkeypatch.setattr(angel_fetcher, "is_configured", lambda: False)
    monkeypatch.setattr(fetcher, "_fetch_stooq", _boom)
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct",
                        lambda t, period, interval: _good_df())

    with caplog.at_level(logging.WARNING, logger="data.fetcher"):
        for i in range(fetcher._STOOQ_BREAKER_THRESHOLD):
            fetcher._FETCH_CACHE.clear()
            fetcher.fetch_single(f"FAKE{i}.NS", period="1y")

    assert fetcher._stooq_breaker_is_tripped()
    assert any("circuit breaker TRIPPED" in r.getMessage() for r in caplog.records)


def test_tripped_breaker_skips_stooq_entirely(monkeypatch):
    """Once tripped, _fetch_stooq must not be called at all — straight to Yahoo."""
    from data import fetcher, angel_fetcher
    monkeypatch.setattr(angel_fetcher, "is_configured", lambda: False)
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct",
                        lambda t, period, interval: _good_df())

    calls = {"n": 0}

    def _counting_boom(*a, **k):
        calls["n"] += 1
        raise ConnectionError("simulated Stooq outage")

    monkeypatch.setattr(fetcher, "_fetch_stooq", _counting_boom)
    for i in range(fetcher._STOOQ_BREAKER_THRESHOLD):
        fetcher._FETCH_CACHE.clear()
        fetcher.fetch_single(f"FAKE{i}.NS", period="1y")
    assert calls["n"] == fetcher._STOOQ_BREAKER_THRESHOLD

    # Breaker should now be tripped — further calls must skip Stooq entirely.
    fetcher._FETCH_CACHE.clear()
    df = fetcher.fetch_single("FAKEX.NS", period="1y")
    assert not df.empty
    assert calls["n"] == fetcher._STOOQ_BREAKER_THRESHOLD  # unchanged — Stooq skipped


def test_success_resets_consecutive_failure_count(monkeypatch):
    """A Stooq success in between failures resets the counter — no premature trip."""
    from data import fetcher, angel_fetcher
    monkeypatch.setattr(angel_fetcher, "is_configured", lambda: False)
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct",
                        lambda t, period, interval: _good_df())

    def _fail(t, period="1y"):
        raise ConnectionError("simulated Stooq outage")

    def _ok(t, period="1y"):
        return _good_df()

    sequence = [_fail, _fail, _ok, _fail, _fail]
    idx = {"i": 0}

    def _dispatch(t, period="1y"):
        fn = sequence[idx["i"]]
        idx["i"] += 1
        return fn(t, period=period)

    monkeypatch.setattr(fetcher, "_fetch_stooq", _dispatch)
    for i in range(len(sequence)):
        fetcher._FETCH_CACHE.clear()
        fetcher.fetch_single(f"FAKE{i}.NS", period="1y")

    # 2 fails, 1 success (reset), 2 fails = only 2 consecutive at the end — not tripped.
    assert not fetcher._stooq_breaker_is_tripped()


@pytest.mark.stooq_off
def test_stooq_skipped_by_default(monkeypatch):
    """FIX STOOQ-OFF: with NSE_ENABLE_STOOQ unset, Stooq is never called."""
    from data import fetcher, angel_fetcher
    calls = {"n": 0}

    def _counting(*a, **k):
        calls["n"] += 1
        return _good_df()

    monkeypatch.setattr(angel_fetcher, "is_configured", lambda: False)
    monkeypatch.setattr(fetcher, "_fetch_stooq", _counting)
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct", lambda *a, **k: _good_df())
    df = fetcher.fetch_single("FAKEOFF.NS", period="1y")
    assert not df.empty
    assert calls["n"] == 0
    assert fetcher._stooq_enabled() is False
