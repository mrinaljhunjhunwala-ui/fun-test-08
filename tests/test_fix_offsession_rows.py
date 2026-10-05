"""
tools/fix_offsession_rows.py — repair of pre-session-dating rows logged on a
weekend or NSE holiday (2026-10-06). Per-test SQLite only (tests/conftest.py);
the post-apply refill is stubbed, so no network.
"""
from __future__ import annotations

import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import trade_store  # noqa: E402
from analysis import verdict_ledger as vl  # noqa: E402
from tools import backfill_ledger as bl  # noqa: E402
from tools import fix_offsession_rows as fx  # noqa: E402

FRI, SAT, SUN, HOL = "2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14"   # 14 Sep: Ganesh Chaturthi


def _seed(ticker, day, at, source="tomorrow_watchlist", horizon="medium", filled=True):
    vl.ensure_schema()
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        rid = con.execute(
            "INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, verdict, horizon, "
            "source, backfill_misses) VALUES (?,?,?,100,'WATCH',?,?,2)",
            (f"{day}T{at}", day, ticker, horizon, source)).lastrowid
        con.execute("INSERT INTO verdict_signal_tags (verdict_log_id, tag) VALUES (?, 'tw:x')", (rid,))
        if filled:
            con.execute("INSERT INTO verdict_forward_returns (verdict_log_id, checked_at, price_1d, "
                        "ret_1d, nifty_ret_1d) VALUES (?, 'x', 101, 1, 0)", (rid,))
    return rid


def _rows():
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        return sorted(con.execute(
            "SELECT v.id, v.logged_date, v.ticker, v.source, v.backfill_misses, "
            "(SELECT COUNT(*) FROM verdict_forward_returns r WHERE r.verdict_log_id = v.id), "
            "(SELECT COUNT(*) FROM verdict_signal_tags t WHERE t.verdict_log_id = v.id) "
            "FROM verdict_log v").fetchall())


def _orphans():
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        return [con.execute(f"SELECT COUNT(*) FROM {t} WHERE verdict_log_id NOT IN "
                            f"(SELECT id FROM verdict_log)").fetchone()[0]
                for t in ("verdict_forward_returns", "verdict_signal_tags")]


def _plan():
    with trade_store._get_conn() as conn:
        return fx.find_plan(conn.cursor(), trade_store)


def _seed_mix():
    ids = {}
    ids["fri"] = _seed("A.NS", FRI, "20:00:00")
    ids["sat_dup"] = _seed("A.NS", SAT, "10:00:00")                 # same pick as Friday's
    ids["sat"] = _seed("B.NS", SAT, "09:00:00")                     # no Friday row
    ids["sun_dup"] = _seed("B.NS", SUN, "09:00:00")                 # same key as B's Saturday row
    ids["hol"] = _seed("C.NS", HOL, "08:00:00")
    ids["other_src"] = _seed("A.NS", SAT, "11:00:00", source="analyze_stock")   # different key
    ids["mon"] = _seed("D.NS", "2026-09-15", "20:00:00")            # session-dated: untouched
    return ids


def test_plan_relabels_unique_rows_and_drops_duplicates():
    ids = _seed_mix()
    plan = _plan()
    assert {r[0]: str(r[2]) for r in plan["relabel"]} == {
        ids["sat"]: FRI, ids["hol"]: FRI, ids["other_src"]: FRI}
    assert sorted(r[0] for r in plan["delete"]) == sorted([ids["sat_dup"], ids["sun_dup"]])


def test_dry_run_changes_nothing(capsys):
    _seed_mix()
    before = _rows()
    assert fx.main([]) == 0
    assert _rows() == before
    out = capsys.readouterr().out
    assert "relabel 3 row(s), delete 2 duplicate(s)" in out and "dry run" in out


def test_apply_repairs_rows_and_refills(monkeypatch, capsys, tmp_path):
    import json
    ids = _seed_mix()
    calls = []
    monkeypatch.setattr(bl, "run_backfill", lambda **k: calls.append(1) or {
        "passes": 1, "scanned": 3, "updated": 3, "outage": False,
        "sector_candidates": 0, "sector_updated": 0})
    backup = tmp_path / "b.json"
    assert fx.main(["--apply", "--backup", str(backup)]) == 0
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert len(saved["verdict_log"]) == 5 and len(saved["verdict_signal_tags"]) == 5
    assert len(saved["verdict_forward_returns"]) == 5
    got = {r[0]: r[1:] for r in _rows()}
    assert ids["sat_dup"] not in got and ids["sun_dup"] not in got
    # relabelled: moved to the entry session, misses reset, returns dropped for recompute, tags kept
    for k in ("sat", "hol", "other_src"):
        assert got[ids[k]][0] == FRI and got[ids[k]][3] == 0 and got[ids[k]][4] == 0 and got[ids[k]][5] == 1
    # untouched rows keep their returns and misses
    assert got[ids["fri"]] == (FRI, "A.NS", "tomorrow_watchlist", 2, 1, 1)
    assert got[ids["mon"]][0] == "2026-09-15" and got[ids["mon"]][4] == 1
    assert _orphans() == [0, 0]
    assert calls == [1]
    assert "off-session rows left: 0" in capsys.readouterr().out
    assert _plan() == {"relabel": [], "delete": []}                  # idempotent


def test_relabelled_row_backfills_on_the_entry_session(monkeypatch):
    import datetime as dt

    import pandas as pd
    rid = _seed("SAT.NS", SAT, "10:00:00")
    with trade_store._get_conn() as conn:
        fx.apply_plan(conn, trade_store, _plan())
    idx = pd.bdate_range("2026-09-01", "2026-09-25")
    monkeypatch.setattr(vl, "_fetch_history",
                        lambda t: pd.DataFrame({"Close": [100.0 + i for i in range(len(idx))]}, index=idx))
    monkeypatch.setattr(vl, "last_closed_session", lambda now=None: dt.date(2026, 9, 25))
    assert vl.backfill_returns()["updated"] == 1
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        nifty_1d, = con.execute("SELECT nifty_ret_1d FROM verdict_forward_returns "
                                "WHERE verdict_log_id = ?", (rid,)).fetchone()
    assert nifty_1d != 0                                               # Fri 11 → Tue 15 Sep
