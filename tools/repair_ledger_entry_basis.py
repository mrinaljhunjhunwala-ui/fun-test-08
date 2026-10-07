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

Usage (targets whatever trade_store resolves: DATABASE_URL, else local SQLite):
    py tools/repair_ledger_entry_basis.py            # dry run — read-only
    py tools/repair_ledger_entry_basis.py --apply    # one transaction
"""
from __future__ import annotations

import argparse
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


def find_plan(cur, store, fetch=None) -> Dict[str, list]:
    """Read-only. Returns {"update": [(id, ticker, {ret_col: new})],
    "no_history": [(id, ticker)], "candidates": n}."""
    from analysis import verdict_ledger as vl

    fetch = fetch or vl._fetch_history
    cols = ", ".join(f"r.price_{h}d, r.ret_{h}d" for h in vl.HORIZONS_DAYS)
    cur.execute(store._q(
        f"SELECT v.id, v.ticker, v.logged_date, v.entry_price, {cols} "
        "FROM verdict_log v JOIN verdict_forward_returns r ON r.verdict_log_id = v.id "
        "WHERE v.entry_price IS NOT NULL AND v.entry_price > 0"))
    rows = cur.fetchall()

    cands = []
    for row in rows:
        rid, ticker, logged, entry = row[0], row[1], row[2], float(row[3])
        pairs = {}
        for i, h in enumerate(vl.HORIZONS_DAYS):
            price, ret = row[4 + 2 * i], row[5 + 2 * i]
            imp = _implied_entry(price, ret)
            if imp is not None:
                pairs[h] = (float(price), imp)
        if any(_same(imp, entry) for _, imp in pairs.values()):
            cands.append((rid, str(ticker), str(logged)[:10], pairs))

    plan: Dict[str, list] = {"update": [], "no_history": [], "candidates": len(cands)}
    hist: Dict[str, object] = {}
    for rid, ticker, logged, pairs in cands:
        if ticker not in hist:
            hist[ticker] = fetch(ticker)
        anchor = vl._entry_session(_dt.date.fromisoformat(logged))
        close = vl._session_close(hist[ticker], anchor)
        if close is None or close <= 0:
            plan["no_history"].append((rid, ticker))
            continue
        upd = {f"ret_{h}d": round((price / close - 1.0) * 100.0, 4)
               for h, (price, imp) in pairs.items() if not _same(imp, close)}
        if upd:
            plan["update"].append((rid, ticker, upd))
    return plan


def apply_plan(conn, store, plan: Dict[str, list]) -> None:
    """Apply every update in ONE transaction."""
    cur = conn.cursor()
    for rid, _ticker, upd in plan["update"]:
        sets = ", ".join(f"{k} = ?" for k in upd)
        cur.execute(store._q(f"UPDATE verdict_forward_returns SET {sets} "
                             "WHERE verdict_log_id = ?"), (*upd.values(), rid))
    conn.commit()


def _summary(plan: Dict[str, list]) -> str:
    n_cols = sum(len(u) for _, _, u in plan["update"])
    return (f"candidates (stored ret based on logged entry): {plan['candidates']}\n"
            f"rows to update: {len(plan['update'])} ({n_cols} ret column(s))\n"
            f"skipped, history no longer reaches entry session: {len(plan['no_history'])}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="apply the plan (default: dry run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    args = ap.parse_args(argv)

    import trade_store as store
    from analysis import verdict_ledger as vl

    vl.ensure_schema()
    print(f"target: {'Postgres (DATABASE_URL)' if store._is_pg() else store._SQLITE_PATH}")
    apply = args.apply and not args.dry_run
    with store._get_conn() as conn:
        try:
            plan = find_plan(conn.cursor(), store)
            print(_summary(plan))
            if not apply or not plan["update"]:
                conn.rollback()
                if not apply:
                    print("dry run — nothing changed. Re-run with --apply.")
                return 0
            apply_plan(conn, store, plan)
        except Exception:
            conn.rollback()
            raise
    print(f"applied: {len(plan['update'])} row(s) updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
