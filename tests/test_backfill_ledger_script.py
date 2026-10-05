"""
tools/backfill_ledger.py — the scheduled ledger backfill (2026-10-05).

Forward returns used to fill only on Calibration page renders, 200 rows each;
Tomorrow's Watchlist rows from 25 Sep on sat at NULL ret_1d/ret_5d for 10 days.
These run against the per-test SQLite file (tests/conftest.py) with price
history stubbed — no network, no Postgres.
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
from tools import backfill_ledger as bl  # noqa: E402

TODAY = dt.date(2026, 10, 5)          # last closed session (Mon)


def _history(ticker):
    idx = pd.bdate_range("2026-08-01", TODAY)
    return pd.DataFrame({"Close": [100.0 + i for i in range(len(idx))]}, index=idx)


def _close(day):
    return float(_history("X").loc[pd.Timestamp(day), "Close"])


def _patch(monkeypatch, fetch=_history):
    monkeypatch.setattr(vl, "last_closed_session", lambda now=None: TODAY)
    monkeypatch.setattr(vl, "_fetch_history", fetch)


def _seed(rows):
    """rows: (logged_at, logged_date, ticker, entry_price, source)."""
    vl.ensure_schema()
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        con.executemany(
            "INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, "
            "verdict, horizon, source) VALUES (?,?,?,?,'WATCH','medium',?)", rows)


def _query(sql):
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        return con.execute(sql).fetchall()


def _quiet(*_a, **_k):
    pass


def test_passes_run_until_every_reachable_row_is_tried(monkeypatch):
    _patch(monkeypatch)
    _seed([(f"2026-09-25T20:{i // 60:02d}:{i % 60:02d}", "2026-09-25", f"T{i}.NS", 100.0,
            "tomorrow_watchlist") for i in range(450)])

    t = bl.run_backfill(batch=200, log=_quiet)

    assert (t["passes"], t["scanned"], t["updated"], t["outage"]) == (3, 450, 450, False)
    assert _query("SELECT COUNT(*) FROM verdict_forward_returns "
                  "WHERE ret_1d IS NOT NULL AND ret_5d IS NOT NULL") == [(450,)]
    assert bl.run_backfill(log=_quiet)["scanned"] == 0     # next run: nothing left


def test_stuck_newer_rows_do_not_starve_watchlist_rows(monkeypatch):
    # 250 newer rows from another source whose ticker history is gone fill
    # nothing. Stopping at the first updated==0 pass (or re-picking the same
    # newest 200 every pass) would never reach the watchlist rows behind them.
    _patch(monkeypatch, lambda t: None if t.startswith("DEAD") else _history(t))
    dead = [(f"2026-10-01T20:{i // 60:02d}:{i % 60:02d}", "2026-10-01", f"DEAD{i}.NS", 100.0,
             "analyze_stock") for i in range(250)]
    tw = [(f"2026-09-25T20:00:{i:02d}", "2026-09-25", f"TW{i}.NS", 100.0,
           "tomorrow_watchlist") for i in range(30)]
    _seed(tw + dead)

    t = bl.run_backfill(batch=200, log=_quiet)

    assert t["updated"] == 30 and t["scanned"] == 280
    assert _query("SELECT COUNT(*) FROM verdict_log v JOIN verdict_forward_returns r "
                  "ON r.verdict_log_id = v.id WHERE v.source = 'tomorrow_watchlist' "
                  "AND r.ret_1d IS NOT NULL AND r.ret_5d IS NOT NULL") == [(30,)]
    # one miss per run, not the whole retry budget in one run
    assert _query("SELECT DISTINCT backfill_misses FROM verdict_log "
                  "WHERE ticker LIKE 'DEAD%'") == [(1,)]


def test_outage_stops_without_misses_and_fails_the_job(monkeypatch, capsys):
    _patch(monkeypatch, lambda t: None)                    # NIFTY down too
    _seed([("2026-09-25T20:00:00", "2026-09-25", "LIVE.NS", 100.0, "tomorrow_watchlist")])

    assert bl.main([]) == 1
    out = capsys.readouterr().out
    assert "SQLite" in out and "provider or network down" in out
    assert _query("SELECT backfill_misses FROM verdict_log") in ([(0,)], [(None,)])


def test_main_reports_sqlite_target_and_succeeds(monkeypatch, capsys):
    _patch(monkeypatch)
    _seed([("2026-09-25T20:00:00", "2026-09-25", "OK.NS", 100.0, "tomorrow_watchlist")])
    assert bl.main([]) == 0
    out = capsys.readouterr().out
    assert os.path.abspath(trade_store._SQLITE_PATH) in out
    assert "forward returns: 1 of 1 rows updated in 1 pass(es)" in out


def test_weekend_logged_row_measures_nifty_from_the_same_session(monkeypatch):
    # Pre-d5d7a3b rows can be dated Sat/Sun; their entry is Friday's close.
    # NIFTY's entry used to be "first bar on-or-after Saturday" = Monday, so
    # nifty_ret_1d was 0 against a stock return measured Fri → Mon.
    _patch(monkeypatch)
    fri = _close("2026-09-25")
    _seed([("2026-09-26T10:00:00", "2026-09-26", "SAT.NS", fri, "tomorrow_watchlist"),
           ("2026-09-27T10:00:00", "2026-09-27", "SUN.NS", None, "tomorrow_watchlist")])

    bl.run_backfill(log=_quiet)

    expected_1d = round((_close("2026-09-28") / fri - 1) * 100, 4)
    expected_5d = round((_close("2026-10-05") / fri - 1) * 100, 4)   # 2 Oct is a holiday
    got = _query("SELECT v.ticker, r.ret_1d, r.nifty_ret_1d, r.ret_5d, r.nifty_ret_5d "
                 "FROM verdict_log v JOIN verdict_forward_returns r ON r.verdict_log_id = v.id "
                 "ORDER BY v.ticker")
    assert got == [(t, expected_1d, expected_1d, expected_5d, expected_5d)
                   for t in ("SAT.NS", "SUN.NS")]
    assert expected_1d != 0


def test_candidate_cap_only_counts_rows_with_a_reached_gap(monkeypatch):
    # Every row under a year old has a NULL 250d price. With the cap applied
    # before the reached test, newer rows whose remaining gaps are unreached
    # filled the whole candidate window and the old row was never selected.
    _patch(monkeypatch)
    monkeypatch.setattr(vl, "_BACKFILL_CANDIDATE_CAP", 10)
    _seed([("2026-09-01T20:00:00", "2026-09-01", "OLD.NS", 100.0, "tomorrow_watchlist")]
          + [(f"2026-09-25T20:00:{i:02d}", "2026-09-25", f"NEW{i}.NS", 100.0,
              "tomorrow_watchlist") for i in range(20)])
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        con.execute("INSERT INTO verdict_forward_returns (verdict_log_id, checked_at, "
                    "price_1d, ret_1d, price_5d, ret_5d) SELECT id, 'x', 1, 0, 1, 0 "
                    "FROM verdict_log WHERE ticker LIKE 'NEW%'")
        # OLD's 1d/5d are done; its 20d target (30 Sep) is the only reached gap
        con.execute("INSERT INTO verdict_forward_returns (verdict_log_id, checked_at, "
                    "price_1d, ret_1d, price_5d, ret_5d) SELECT id, 'x', 1, 0, 1, 0 "
                    "FROM verdict_log WHERE ticker = 'OLD.NS'")

    stats = vl.backfill_returns()

    assert (stats["scanned"], stats["updated"]) == (1, 1)
    assert _query("SELECT r.price_20d IS NOT NULL FROM verdict_log v JOIN "
                  "verdict_forward_returns r ON r.verdict_log_id = v.id "
                  "WHERE v.ticker = 'OLD.NS'") == [(1,)]


def test_reached_cutoff_matches_horizon_targets():
    days = pd.date_range("2026-08-20", TODAY).date
    for h in vl.HORIZONS_DAYS[:3]:
        cut = vl._reached_cutoff(TODAY, h)
        for d in days:
            assert (d < cut) == (vl.horizon_target_date(d, h) <= TODAY), (h, d)
