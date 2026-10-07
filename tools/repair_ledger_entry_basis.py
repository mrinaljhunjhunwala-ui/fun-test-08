"""
tools/repair_ledger_entry_basis.py — recompute stored ret_* that were measured
from the LOGGED entry price instead of the entry session's close.

Background (review of #304): backfill_returns() measured the stock leg from
verdict_log.entry_price (whatever the scan saw, often intraday) but the NIFTY
leg from the entry session's close, so "edge vs Nifty" mixed an intraday entry
with a close-to-close benchmark. The backfill now measures both legs from the
entry session's close. This tool fixes rows filled before that change.

A row is a candidate when, for some filled horizon, the entry implied by the
stored price and return (price_h / (1 + ret_h/100)) matches the logged
entry_price but not the entry session's close. Its ret_h is recomputed as
price_h / session_close - 1 (price_h, the exit close, is unchanged). NIFTY and
sector returns were already close-to-close and are left alone.

Idempotent: a repaired row's implied entry equals the session close, so a
second run touches nothing. Rows whose history no longer reaches the entry
session are skipped and reported.

Plan first, write second: candidates are read on a read-only connection that
is released before any network fetch; the updates are then applied in one
short transaction (no transaction is held open across the fetches). A dry run
never opens a write transaction and never runs schema DDL.

Usage (targets whatever trade_store resolves: DATABASE_URL, else local SQLite):
    py tools/repair_ledger_entry_basis.py                       # dry run — read-only
    py tools/repair_ledger_entry_basis.py --dry-run --csv x.csv # + per-row CSV
    py tools/repair_ledger_entry_basis.py --apply               # one short transaction
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import sys
from typing import Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Relative tolerance for "these two prices are the same" (stored values are
# rounded to 4 dp, so an exact compare would miss).
_REL_TOL = 1e-4


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= _REL_TOL * max(abs(a), abs(b), 1e-9)


def _implied_entry(price, ret) -> Optional[float]:
    try:
        price, ret = float(price), float(ret)
    except (TypeError, ValueError):
        return None
    if price != price or ret != ret or ret <= -100.0:   # NaN guard
        return None
    return price / (1.0 + ret / 100.0)


def load_candidates(cur, store) -> list:
    """Read-only SQL. Rows whose stored ret_h implies the LOGGED entry price:
    [(id, ticker, logged_date, {h: (price_h, ret_h, implied_entry)})]."""
    from analysis import verdict_ledger as vl

    cols = ", ".join(f"r.price_{h}d, r.ret_{h}d" for h in vl.HORIZONS_DAYS)
    cur.execute(store._q(
        f"SELECT v.id, v.ticker, v.logged_date, v.entry_price, {cols} "
        "FROM verdict_log v JOIN verdict_forward_returns r ON r.verdict_log_id = v.id "
        "WHERE v.entry_price IS NOT NULL AND v.entry_price > 0 ORDER BY v.id"))
    cands = []
    for row in cur.fetchall():
        rid, ticker, logged, entry = row[0], row[1], row[2], float(row[3])
        pairs = {}
        for i, h in enumerate(vl.HORIZONS_DAYS):
            price, ret = row[4 + 2 * i], row[5 + 2 * i]
            imp = _implied_entry(price, ret)
            if imp is not None:
                pairs[h] = (float(price), float(ret), imp)
        if any(_same(imp, entry) for _, _, imp in pairs.values()):
            cands.append((int(rid), str(ticker), str(logged)[:10], pairs))
    return cands


def plan_from_candidates(cands: list, fetch=None) -> Dict[str, list]:
    """Network only (no DB). Returns {"update": [(id, ticker, {ret_col: new})],
    "detail": [dict per changed (row, horizon)], "no_history": [(id, ticker)],
    "candidates": n}."""
    from analysis import verdict_ledger as vl

    fetch = fetch or vl._fetch_history
    plan: Dict[str, list] = {"update": [], "detail": [], "no_history": [],
                             "candidates": len(cands), "changed_since_plan": 0}
    hist: Dict[str, object] = {}
    for rid, ticker, logged, pairs in cands:
        if ticker not in hist:
            hist[ticker] = fetch(ticker)
        anchor = vl._entry_session(_dt.date.fromisoformat(logged))
        close = vl._session_close(hist[ticker], anchor)
        if close is None or close <= 0:
            plan["no_history"].append((rid, ticker))
            continue
        upd, old_vals = {}, {}
        for h, (price, old, imp) in sorted(pairs.items()):
            if _same(imp, close):
                continue
            new = round((price / close - 1.0) * 100.0, 4)
            upd[f"ret_{h}d"] = new
            old_vals[f"ret_{h}d"] = old
            plan["detail"].append({
                "id": rid, "ticker": ticker, "logged_date": logged, "horizon": h,
                "old_ret": round(old, 4), "new_ret": new,
                "old_implied_entry": round(imp, 4), "session_close": round(float(close), 4)})
        if upd:
            plan["update"].append((rid, ticker, upd, old_vals))
    return plan


def find_plan(cur, store, fetch=None) -> Dict[str, list]:
    """load_candidates + plan_from_candidates (kept for callers/tests)."""
    return plan_from_candidates(load_candidates(cur, store), fetch)


# A stored ret still "is" the planned old value within this tolerance.
_OLD_TOL = 1e-6


def apply_plan(conn, store, plan: Dict[str, list]) -> int:
    """Apply every update in ONE transaction. Each UPDATE only fires while every
    column it rewrites still holds the value seen at planning time, so a row
    re-filled in between is left alone. Returns rows actually updated and sets
    plan["changed_since_plan"]."""
    cur = conn.cursor()
    done = 0
    for rid, _ticker, upd, old in plan["update"]:
        sets = ", ".join(f"{k} = ?" for k in upd)
        guard = " AND ".join(f"ABS({k} - ?) < {_OLD_TOL}" for k in upd)
        cur.execute(store._q(f"UPDATE verdict_forward_returns SET {sets} "
                             f"WHERE verdict_log_id = ? AND {guard}"),
                    (*upd.values(), rid, *(old[k] for k in upd)))
        done += 1 if (cur.rowcount or 0) > 0 else 0
    conn.commit()
    plan["changed_since_plan"] = len(plan["update"]) - done
    return done


_FIELDS = ["id", "ticker", "logged_date", "horizon", "old_ret", "new_ret",
           "old_implied_entry", "session_close"]


def format_table(detail: list) -> str:
    rows = [_FIELDS] + [[str(d[f]) for f in _FIELDS] for d in detail]
    widths = [max(len(r[i]) for r in rows) for i in range(len(_FIELDS))]
    return "\n".join("  ".join(c.rjust(w) for c, w in zip(r, widths)) for r in rows)


def write_csv(path: str, detail: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_FIELDS)
        w.writeheader()
        w.writerows(detail)


def _summary(plan: Dict[str, list]) -> str:
    n_cols = sum(len(u) for _, _, u, _ in plan["update"])
    return (f"candidates (stored ret based on logged entry): {plan['candidates']}\n"
            f"rows to update: {len(plan['update'])} ({n_cols} ret column(s))\n"
            f"skipped, history no longer reaches entry session: {len(plan['no_history'])}")


def _tables_exist(cur, store) -> bool:
    """Both ledger tables exist (a fresh DB has none: 0 candidates, no DDL)."""
    names = ("verdict_log", "verdict_forward_returns")
    if store._is_pg():
        cur.execute("SELECT to_regclass(%s), to_regclass(%s)", names)
        return all(cur.fetchone())
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?, ?)", names)
    return cur.fetchone()[0] == 2


def _read_candidates(store) -> list:
    """Candidates on a connection that never writes: Postgres gets a READ ONLY
    transaction; both backends roll back and release before any fetch."""
    with store._get_conn() as conn:
        try:
            cur = conn.cursor()
            if store._is_pg():
                cur.execute("SET TRANSACTION READ ONLY")
            if not _tables_exist(cur, store):
                return []
            return load_candidates(cur, store)
        finally:
            conn.rollback()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="apply the plan (default: dry run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    ap.add_argument("--csv", metavar="PATH", help="also write the per-row plan to a CSV")
    args = ap.parse_args(argv)

    import trade_store as store
    from analysis import verdict_ledger as vl

    apply = args.apply and not args.dry_run
    print(f"target: {'Postgres (DATABASE_URL)' if store._is_pg() else store._SQLITE_PATH}")
    if apply:
        vl.ensure_schema()
    # 1. plan: read-only SQL, connection released; 2. network fetches, no DB held.
    plan = plan_from_candidates(_read_candidates(store))
    if plan["detail"]:
        print(format_table(plan["detail"]))
    if args.csv:
        write_csv(args.csv, plan["detail"])
        print(f"csv: {args.csv} ({len(plan['detail'])} row(s))")
    print(_summary(plan))
    if not apply:
        print("dry run — nothing changed. Re-run with --apply.")
        return 0
    if not plan["update"]:
        return 0
    # 3. apply: one short write transaction.
    with store._get_conn() as conn:
        try:
            done = apply_plan(conn, store, plan)
        except Exception:
            conn.rollback()
            raise
    print(f"applied: {done} of {len(plan['update'])} planned row(s) updated.")
    print(f"skipped {plan['changed_since_plan']} rows changed since planning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
