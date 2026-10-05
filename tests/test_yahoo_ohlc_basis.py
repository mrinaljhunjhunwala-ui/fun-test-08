"""Yahoo bars must be internally consistent (audit 2026-10-03, provenance canary).

Close came from `adjclose` (dividend-adjusted) while Open/High/Low were raw:
HDFCBANK's close sat outside [Low, High] on 403 of 500 bars and ATR read ~69%
high. Every column now comes from the raw quote series (already split/bonus
adjusted by Yahoo), and daily flat zero-volume filler bars are dropped.
"""
import datetime as dt

import numpy as np

from data.fetcher import _parse_yahoo_chart


def _ts(d):
    return int(dt.datetime(*d, 3, 45, tzinfo=dt.timezone.utc).timestamp())   # 09:15 IST


def _payload(rows, adj_factor=0.97):
    """rows: (date tuple, open, high, low, close, volume)."""
    return {"chart": {"result": [{
        "timestamp": [_ts(r[0]) for r in rows],
        "indicators": {
            "quote": [{"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                       "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                       "volume": [r[5] for r in rows]}],
            "adjclose": [{"adjclose": [r[4] * adj_factor for r in rows]}],
        },
    }], "error": None}}


_ROWS = [
    ((2026, 9, 29), 960.0, 970.0, 955.0, 965.25, 1_000_000),
    ((2026, 9, 30), 966.0, 975.0, 960.0, 972.00, 1_200_000),
    ((2026, 10, 1), 971.0, 980.0, 968.0, 978.50, 900_000),
]


def test_close_is_the_raw_exchange_close_not_adjclose():
    df = _parse_yahoo_chart(_payload(_ROWS), "HDFCBANK.NS", "1d")
    assert list(df["Close"]) == [965.25, 972.00, 978.50]


def test_every_close_sits_inside_its_bar():
    df = _parse_yahoo_chart(_payload(_ROWS, adj_factor=0.95), "HDFCBANK.NS", "1d")
    assert ((df["Close"] <= df["High"]) & (df["Close"] >= df["Low"])).all()


def test_daily_flat_zero_volume_holiday_bar_is_dropped():
    rows = _ROWS[:2] + [((2026, 10, 2), 972.0, 972.0, 972.0, 972.0, 0)] + _ROWS[2:]
    df = _parse_yahoo_chart(_payload(rows), "HDFCBANK.NS", "1d")
    assert dt.date(2026, 10, 2) not in {d.date() for d in df.index}
    assert len(df) == 3


def test_index_bars_with_zero_volume_are_kept_when_they_move():
    rows = [(d, o, h, lo, c, 0) for d, o, h, lo, c, _v in _ROWS]   # ^NSEI reports no volume
    df = _parse_yahoo_chart(_payload(rows), "^NSEI", "1d")
    assert len(df) == 3


def test_intraday_zero_volume_bars_are_kept():
    rows = [((2026, 10, 1), 100.0, 100.0, 100.0, 100.0, 0),
            ((2026, 10, 1), 100.0, 101.0, 99.5, 100.5, 500)]
    payload = _payload(rows)
    payload["chart"]["result"][0]["timestamp"] = [_ts((2026, 10, 1)), _ts((2026, 10, 1)) + 300]
    df = _parse_yahoo_chart(payload, "X.NS", "5m")
    assert len(df) == 2


def test_missing_adjclose_block_is_fine():
    payload = _payload(_ROWS)
    del payload["chart"]["result"][0]["indicators"]["adjclose"]
    df = _parse_yahoo_chart(payload, "X.NS", "1d")
    assert np.allclose(df["Close"], [965.25, 972.00, 978.50])
