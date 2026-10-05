"""
tools/fix_offsession_rows.py — one-off repair of verdict_log rows dated a
weekend or NSE holiday.

Background (2026-10-06): before session dating (d5d7a3b, 2026-09-27)
logged_date was the server's wall-clock date, so scans on a Saturday, Sunday
or holiday were dated that day although they priced off the previous
session's close. Two consequences:
  * duplicates — a Saturday row and the Friday-evening row for the same
    (ticker, horizon, source) are the same pick at the same close, so
    Tomorrow's Watchlist's hit rate counted it twice;
  * wrong benchmark — until #280 the NIFTY entry for those rows was the NEXT
    session's close (nifty_ret_1d = 0 against a stock return from Friday),
    and until #283 their sector-basket returns never filled.

For each off-session row, keyed by (entry session, ticker, horizon, source):
    a session-dated row already holds that key  → delete the off-session row
    otherwise                                    → keep the earliest-logged
        off-session row, move its logged_date to the entry session, reset
        backfill_misses and drop its forward returns so the backfill
        recomputes them on the right anchor; delete any other off-session
        rows for the same key
Children (verdict_signal_tags, verdict_forward_returns) are deleted with
their verdict_log row.

Usage (targets whatever trade_store resolves: DATABASE_URL, else local SQLite):
    py tools/fix_offsession_rows.py            # dry run — read-only
    py tools/fix_offsession_rows.py --apply    # one transaction, then refill
                                               # returns (tools/backfill_ledger)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import tempfile
import sys
from collections import Counter, defaultdict
from typing import Dict, List

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_CHUNK = 500


def find_plan(cur, store) -> Dict[str, list]:
    """Read-only. Returns {"relabel": [(id, old, new, ticker, horizon, source)],
    "delete": [(id, old, ticker, horizon, source, reason)]}."""
    from analysis.verdict_ledger import _entry_session
    from utils.nse_holidays import is_trading_day

    cur.execute("SELECT id, logged_at, logged_date, ticker, horizon, source FROM verdict_log")
    rows = cur.fetchall()
    session_keys = set()
    off: Dict[tuple, list] = defaultdict(list)
    for rid, at, d, ticker, horizon, source in rows:
        day = _dt.date.fromisoformat(str(d)[:10])
        if is_trading_day(day):
            session_keys.add((day, ticker, horizon, source))
        else:
            off[(_entry_session(day), ticker, horizon, source)].append((str(at), rid, day))

    plan: Dict[str, list] = {"relabel": [], "delete": []}
    for key, grp in sorted(off.items(), key=lambda kv: (kv[0][0], kv[0][1:])):
        new, ticker, horizon, source = key
        grp.sort()
        if key in session_keys:
            keep = None
        else:
            keep = grp[0]
            plan["relabel"].append((keep[1], keep[2], new, ticker, horizon, source))
        for _, rid, day in grp:
            if keep is None:
                plan["delete"].append((rid, day, ticker, horizon, source, f"duplicate of {new}"))
            elif rid != keep[1]:
                plan["delete"].append((rid, day, ticker, horizon, source,
                                       f"duplicate of relabelled #{keep[1]}"))
    return plan


def _in(cur, store, sql: str, ids: List[int]) -> None:
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        cur.execute(store._q(sql.format(ph=",".join(["?"] * len(chunk)))), tuple(chunk))


def backup_rows(cur, store, plan: Dict[str, list], path: str) -> int:
    """Write every row the plan touches (verdict_log + both child tables) to
    a JSON file before anything changes. Returns the verdict_log row count."""
    ids = [r[0] for r in plan["delete"]] + [r[0] for r in plan["relabel"]]
    dump: Dict[str, list] = {}
    for table, col in (("verdict_log", "id"), ("verdict_forward_returns", "verdict_log_id"),
                       ("verdict_signal_tags", "verdict_log_id")):
        dump[table] = []
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            cur.execute(store._q(f"SELECT * FROM {table} WHERE {col} IN "
                                 f"({','.join(['?'] * len(chunk))})"), tuple(chunk))
            names = [d[0] for d in cur.description]
            dump[table] += [dict(zip(names, row)) for row in cur.fetchall()]
    dump["plan"] = plan
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dump, f, default=str, indent=1)
    return len(dump["verdict_log"])


def apply_plan(conn, store, plan: Dict[str, list]) -> None:
    """Apply in ONE transaction: deletes (children first), then relabels."""
    cur = conn.cursor()
    dels = [r[0] for r in plan["delete"]]
    rel = [r[0] for r in plan["relabel"]]
    _in(cur, store, "DELETE FROM verdict_signal_tags WHERE verdict_log_id IN ({ph})", dels)
    _in(cur, store, "DELETE FROM verdict_forward_returns WHERE verdict_log_id IN ({ph})", dels + rel)
    _in(cur, store, "DELETE FROM verdict_log WHERE id IN ({ph})", dels)
    for rid, _old, new, *_ in plan["relabel"]:
        cur.execute(store._q("UPDATE verdict_log SET logged_date = ?, backfill_misses = 0 "
                             "WHERE id = ?"), (new.isoformat(), rid))
    conn.commit()


def _summary(plan: Dict[str, list]) -> str:
    out = [f"relabel {len(plan['relabel'])} row(s), delete {len(plan['delete'])} duplicate(s)"]
    by = Counter((str(r[1]), r[5]) for r in plan["relabel"])
    by_del = Counter((str(r[1]), r[4]) for r in plan["delete"])
    for (d, src) in sorted(set(by) | set(by_del)):
        out.append(f"  {d} {src:<22} relabel {by.get((d, src), 0):>4}   delete {by_del.get((d, src), 0):>4}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="apply the plan (default: dry run)")
    ap.add_argument("--backup", default=os.path.join(
        tempfile.gettempdir(), f"verdict_offsession_backup_{_dt.datetime.now():%Y%m%d_%H%M%S}.json"),
        help="where --apply writes the touched rows first")
    args = ap.parse_args(argv)

    import trade_store as store
    from tools.backfill_ledger import _target_label, run_backfill

    print(f"target: {_target_label(store)}")
    readonly = store._is_pg() and not args.apply
    with store._get_conn() as conn:
        if readonly:
            conn.set_session(readonly=True)
        try:
            plan = find_plan(conn.cursor(), store)
            print(_summary(plan))
            if not args.apply or not (plan["relabel"] or plan["delete"]):
                conn.rollback()
                if not args.apply:
                    print("dry run — nothing changed. Re-run with --apply.")
                return 0
            n = backup_rows(conn.cursor(), store, plan, args.backup)
            print(f"backup: {n} verdict_log row(s) + children → {args.backup}")
            apply_plan(conn, store, plan)
            left = find_plan(conn.cursor(), store)
            conn.rollback()
        except Exception:
            conn.rollback()
            raise
        finally:
            if readonly:
                conn.set_session(readonly=False)    # connection goes back to the pool
    print(f"applied. off-session rows left: {len(left['relabel']) + len(left['delete'])}")
    t = run_backfill()
    print(f"refill: {t['updated']} of {t['scanned']} rows, "
          f"sector {t['sector_updated']} of {t['sector_candidates']}")
    return 1 if t["outage"] else 0


if __name__ == "__main__":
    sys.exit(main())
