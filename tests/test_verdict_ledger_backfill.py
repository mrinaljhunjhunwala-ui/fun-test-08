"""
FIX LEDGER-STARVE: backfill_returns() used to take the max_rows NEWEST rows
needing work. Scan pages log 30-60 rows a day, so those were always too recent
for any horizon — production showed "200 scanned, 0 updated" on every
Calibration visit while ~430 older rows never got a 1d/5d return.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import trade_store  # noqa: E402
from analysis import verdict_ledger as vl  # noqa: E402

TODAY = dt.date(2026, 9, 25)          # last closed session (Fri)
OLD = dt.date(2026, 9, 8)             # 1d and 5d horizons reached, 20d not


def _history(ticker):
    idx = pd.bdate_range("2026-08-01", TODAY)
    close = [100.0 + i for i in range(len(idx))]
    return pd.DataFrame({"Close": close}, index=idx)


def _seed(rows):
    vl.ensure_schema()
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        con.executemany(
            "INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, "
            "verdict, horizon, source) VALUES (?,?,?,?,?,?,?)",
            rows,
        )


def _fwd():
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        return con.execute(
            "SELECT v.ticker, r.price_1d, r.price_5d, r.price_20d "
            "FROM verdict_log v JOIN verdict_forward_returns r ON r.verdict_log_id = v.id"
        ).fetchall()


def _patch(monkeypatch):
    monkeypatch.setattr(vl, "last_closed_session", lambda now=None: TODAY)
    monkeypatch.setattr(vl, "_fetch_history", _history)


def test_old_eligible_rows_are_not_starved_by_fresh_rows(monkeypatch):
    _patch(monkeypatch)
    fresh = [(f"2026-09-25T20:{i // 60:02d}:{i % 60:02d}", "2026-09-25", f"NEW{i}.NS",
              100.0, "WATCH", "medium", "tomorrow_watchlist") for i in range(250)]
    old = [(f"2026-09-08T20:00:0{i}", OLD.isoformat(), f"OLD{i}.NS",
            100.0, "WATCH", "medium", "tomorrow_watchlist") for i in range(5)]
    _seed(old + fresh)

    stats = vl.backfill_returns(max_rows=200)

    assert stats["updated"] == 5, stats
    assert stats["scanned"] == 5            # fresh rows never enter the batch
    filled = {t: (p1, p5, p20) for t, p1, p5, p20 in _fwd()}
    assert set(filled) == {f"OLD{i}.NS" for i in range(5)}
    assert all(p1 is not None and p5 is not None and p20 is None for p1, p5, p20 in filled.values())


def test_rows_with_only_unreached_gaps_are_skipped(monkeypatch):
    _patch(monkeypatch)
    _seed([("2026-09-08T20:00:00", OLD.isoformat(), "OLD.NS", 100.0, "WATCH", "medium", "x")])
    assert vl.backfill_returns()["updated"] == 1
    # 1d/5d now filled; 20d/60d/250d are the only gaps and none is reached
    assert vl.backfill_returns() == {"scanned": 0, "updated": 0, "tickers_fetched": 0}


def test_max_rows_still_caps_the_batch(monkeypatch):
    _patch(monkeypatch)
    _seed([(f"2026-09-08T20:00:{i:02d}", OLD.isoformat(), f"OLD{i}.NS",
            100.0, "WATCH", "medium", "x") for i in range(12)])
    assert vl.backfill_returns(max_rows=5)["updated"] == 5
    assert vl.backfill_returns(max_rows=5)["updated"] == 5
    assert vl.backfill_returns(max_rows=5)["updated"] == 2
