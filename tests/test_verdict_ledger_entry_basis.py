"""
FIX LEDGER-BASIS (review of #304): the stock leg of a forward return was
measured from the LOGGED entry_price (often intraday) while NIFTY's leg was
measured from the entry session's close. Both legs now run close-to-close from
the entry session; entry_price stays in verdict_log for display only.
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
from tools import repair_ledger_entry_basis as repair  # noqa: E402

TODAY = dt.date(2026, 9, 25)          # Fri, last closed session
LOGGED = dt.date(2026, 9, 15)         # Tue
STOCK0, NIFTY0 = 100.0, 20000.0


def _history(ticker):
    idx = pd.bdate_range("2026-08-03", TODAY)
    base, step = (NIFTY0, 10.0) if ticker == vl.NIFTY_BENCH else (STOCK0, 1.0)
    return pd.DataFrame({"Close": [base + step * i for i in range(len(idx))]}, index=idx)


def _close(ticker, d):
    return float(_history(ticker).loc[pd.Timestamp(d), "Close"])


def _patch(monkeypatch):
    monkeypatch.setattr(vl, "last_closed_session", lambda now=None: TODAY)
    monkeypatch.setattr(vl, "_fetch_history", _history)


def _seed(rows):
    """rows: (logged_date, ticker, entry_price, verdict)."""
    vl.ensure_schema()
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        con.executemany(
            "INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, "
            "verdict, horizon, source) VALUES (?,?,?,?,?,'medium','tomorrow_watchlist')",
            [(f"{d}T11:00:00", d, t, e, v) for d, t, e, v in rows])


def _rets(ticker):
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        return con.execute(
            "SELECT r.ret_1d, r.nifty_ret_1d, r.ret_5d, r.nifty_ret_5d FROM verdict_log v "
            "JOIN verdict_forward_returns r ON r.verdict_log_id = v.id WHERE v.ticker = ?",
            (ticker,)).fetchone()


def _pct(a, b):
    return round((a / b - 1) * 100, 4)


def test_intraday_logged_row_measures_both_legs_from_the_session_close(monkeypatch):
    _patch(monkeypatch)
    close = _close("INTRA.NS", LOGGED)
    _seed([(LOGGED.isoformat(), "INTRA.NS", close * 0.97, "WATCH")])   # intraday dip
    vl.backfill_returns()
    d1, d5 = vl.horizon_target_date(LOGGED, 1), vl.horizon_target_date(LOGGED, 5)
    n0 = _close(vl.NIFTY_BENCH, LOGGED)
    assert _rets("INTRA.NS") == (
        _pct(_close("INTRA.NS", d1), close), _pct(_close(vl.NIFTY_BENCH, d1), n0),
        _pct(_close("INTRA.NS", d5), close), _pct(_close(vl.NIFTY_BENCH, d5), n0))
    # the logged price is kept for display
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        assert con.execute("SELECT entry_price FROM verdict_log").fetchone()[0] == close * 0.97


def test_weekend_logged_row_anchors_both_legs_on_the_previous_session(monkeypatch):
    _patch(monkeypatch)
    sat, fri = dt.date(2026, 9, 19), dt.date(2026, 9, 18)
    _seed([(sat.isoformat(), "WKND.NS", 123.0, "WATCH")])     # stale/intraday price
    vl.backfill_returns()
    mon = dt.date(2026, 9, 21)
    got = _rets("WKND.NS")
    assert got[0] == _pct(_close("WKND.NS", mon), _close("WKND.NS", fri))
    assert got[1] == _pct(_close(vl.NIFTY_BENCH, mon), _close(vl.NIFTY_BENCH, fri))


def test_live_scorecard_and_calibration_use_close_to_close_alpha(monkeypatch):
    _patch(monkeypatch)
    # Two picks on the same session, logged at prices far from the close —
    # under the old basis their edge would be dominated by the entry gap.
    _seed([(LOGGED.isoformat(), "UP.NS", 50.0, "WATCH"),
           (LOGGED.isoformat(), "DN.NS", 500.0, "AVOID")])
    vl.backfill_returns()
    s_ret = _pct(_close("UP.NS", vl.horizon_target_date(LOGGED, 5)), _close("UP.NS", LOGGED))
    n_ret = _pct(_close(vl.NIFTY_BENCH, vl.horizon_target_date(LOGGED, 5)),
                 _close(vl.NIFTY_BENCH, LOGGED))
    edge = s_ret - n_ret

    sc = vl.live_scorecard(horizon_days=5)
    assert sc["error"] is None and sc["pending"] == 0
    summ = sc["summary"].set_index("side")
    assert summ.loc["bullish", "mean_edge_pct"] == round(edge, 2)
    assert summ.loc["bearish", "mean_edge_pct"] == round(-edge, 2)
    assert summ.loc["all", "picks"] == 2

    cal = vl.calibration_by(group_col="verdict", horizon_days=5).set_index("verdict")
    assert cal.loc["WATCH", "mean_ret"] == round(s_ret, 2)
    assert cal.loc["WATCH", "mean_alpha"] == round(edge, 2)
    assert cal.loc["AVOID", "n"] == 1


def test_repair_tool_recomputes_intraday_based_rows_idempotently(monkeypatch, capsys):
    _patch(monkeypatch)
    close = _close("OLD.NS", LOGGED)
    _seed([(LOGGED.isoformat(), "OLD.NS", close * 1.02, "WATCH"),   # filled the old way
           (LOGGED.isoformat(), "OK.NS", close * 1.02, "WATCH")])   # filled the new way
    vl.backfill_returns()
    # Rewrite OLD.NS's stored returns as the pre-fix backfill computed them.
    p1 = _close("OLD.NS", vl.horizon_target_date(LOGGED, 1))
    p5 = _close("OLD.NS", vl.horizon_target_date(LOGGED, 5))
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        con.execute("UPDATE verdict_forward_returns SET ret_1d=?, ret_5d=? WHERE verdict_log_id="
                    "(SELECT id FROM verdict_log WHERE ticker='OLD.NS')",
                    (_pct(p1, close * 1.02), _pct(p5, close * 1.02)))
    good = _rets("OK.NS")

    assert repair.main([]) == 0                          # dry run by default
    out = capsys.readouterr().out
    assert "rows to update: 1 (2 ret column(s))" in out and "dry run" in out
    assert _rets("OLD.NS")[0] == _pct(p1, close * 1.02)   # untouched

    assert repair.main(["--apply"]) == 0
    assert _rets("OLD.NS") == good                        # same close basis now
    assert _rets("OK.NS") == good
    capsys.readouterr()
    assert repair.main([]) == 0
    assert "rows to update: 0" in capsys.readouterr().out


def test_shadow_pnl_labels_the_logged_price(monkeypatch):
    _patch(monkeypatch)
    vl.ensure_schema()
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        con.execute("INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, "
                    "verdict, horizon, source) VALUES ('2026-08-10T11:00:00','2026-08-10',"
                    "'SH.NS', 90.0, 'BUY', 'medium', 'shadow_auto')")
    vl.backfill_returns()
    sh = vl.shadow_pnl(horizon_days=5)
    assert "price_when_logged" in sh.columns and "entry_price" not in sh.columns
    exp = _pct(_close("SH.NS", vl.horizon_target_date(dt.date(2026, 8, 10), 5)),
               _close("SH.NS", dt.date(2026, 8, 10)))
    assert sh["return_5d_pct"].iloc[0] == exp


def _late(start, end=None):
    def f(t):
        h = _history(t)
        if t == vl.NIFTY_BENCH:
            return h
        return h.loc[pd.Timestamp(start):(pd.Timestamp(end) if end else None)]
    return f


def test_no_entry_bar_skips_row_instead_of_mixing_bases(monkeypatch):
    _patch(monkeypatch)
    # History starts after the entry session: no close for the stock's entry.
    monkeypatch.setattr(vl, "_fetch_history", _late("2026-09-17"))
    _seed([(LOGGED.isoformat(), "LATE.NS", 101.0, "WATCH")])
    stats = vl.backfill_returns()
    assert stats["skipped_no_entry_bar"] == 1 and stats["updated"] == 0
    assert _rets("LATE.NS") is None                  # no ret on a mixed basis
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:   # ticker alive: no miss
        assert (con.execute("SELECT backfill_misses FROM verdict_log").fetchone()[0] or 0) == 0


def test_no_entry_bar_on_dead_ticker_counts_a_miss(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(vl, "_fetch_history", _late("2026-09-16", "2026-09-17"))
    _seed([(LOGGED.isoformat(), "GONE.NS", 101.0, "WATCH")])
    stats = vl.backfill_returns()
    assert stats["skipped_no_entry_bar"] == 1
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        assert con.execute("SELECT backfill_misses FROM verdict_log").fetchone()[0] == 1


def _seed_old_basis():
    close = _close("OLD.NS", LOGGED)
    _seed([(LOGGED.isoformat(), "OLD.NS", close * 1.02, "WATCH")])
    vl.backfill_returns()
    p1 = _close("OLD.NS", vl.horizon_target_date(LOGGED, 1))
    p5 = _close("OLD.NS", vl.horizon_target_date(LOGGED, 5))
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        con.execute("UPDATE verdict_forward_returns SET ret_1d=?, ret_5d=?",
                    (_pct(p1, close * 1.02), _pct(p5, close * 1.02)))
    return close, p1


def test_repair_dry_run_lists_every_row_and_writes_csv(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch)
    close, p1 = _seed_old_basis()
    out_csv = tmp_path / "plan.csv"
    assert repair.main(["--dry-run", "--csv", str(out_csv)]) == 0
    out = capsys.readouterr().out
    assert "old_implied_entry" in out and "session_close" in out and "OLD.NS" in out
    df = pd.read_csv(out_csv)
    assert list(df.columns) == repair._FIELDS
    assert sorted(df["horizon"]) == [1, 5]
    r1 = df.set_index("horizon").loc[1]
    assert r1["old_ret"] == _pct(p1, close * 1.02) and r1["new_ret"] == _pct(p1, close)
    assert abs(r1["old_implied_entry"] - close * 1.02) < 1e-2 and r1["session_close"] == close
    assert _rets("OLD.NS")[0] == _pct(p1, close * 1.02)       # dry run wrote nothing


def test_repair_fetches_with_no_connection_held(monkeypatch, capsys):
    from contextlib import contextmanager

    _patch(monkeypatch)
    _seed_old_basis()
    held = {"n": 0, "max_during_fetch": 0}
    real = trade_store._get_conn

    @contextmanager
    def counting():
        held["n"] += 1
        try:
            with real() as c:
                yield c
        finally:
            held["n"] -= 1

    def fetch(t):
        held["max_during_fetch"] = max(held["max_during_fetch"], held["n"])
        return _history(t)

    monkeypatch.setattr(trade_store, "_get_conn", counting)
    monkeypatch.setattr(vl, "_fetch_history", fetch)
    assert repair.main(["--apply"]) == 0
    assert held["max_during_fetch"] == 0
    capsys.readouterr()
    assert repair.main([]) == 0
    assert "rows to update: 0" in capsys.readouterr().out     # idempotent


def test_repair_leaves_rows_changed_since_planning(monkeypatch, capsys):
    _patch(monkeypatch)
    close, p1 = _seed_old_basis()
    real_plan = repair.plan_from_candidates

    def plan_then_refill(cands, fetch=None):
        plan = real_plan(cands, fetch)
        with sqlite3.connect(trade_store._SQLITE_PATH) as con:   # concurrent re-fill
            con.execute("UPDATE verdict_forward_returns SET ret_1d = 42.0")
        return plan

    monkeypatch.setattr(repair, "plan_from_candidates", plan_then_refill)
    assert repair.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "applied: 0 of 1" in out and "skipped 1 rows changed since planning" in out
    assert _rets("OLD.NS")[0] == 42.0                       # left alone


def test_repair_dry_run_on_missing_tables_finds_nothing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(trade_store, "_SQLITE_PATH", str(tmp_path / "empty.db"))
    assert repair.main(["--dry-run"]) == 0
    assert "candidates (stored ret based on logged entry): 0" in capsys.readouterr().out
