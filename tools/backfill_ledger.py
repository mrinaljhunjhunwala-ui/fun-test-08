"""
tools/backfill_ledger.py — fill the verdict ledger's forward returns after
the close, so nobody has to open the Calibration page for them to happen.

Background (2026-10-05): backfill_returns() / backfill_sector_returns() ran
only when someone rendered dashboard/pages/21_verdict_calibration.py, 200 rows
per visit. Nobody visited between 23 Sep and 5 Oct, so every Tomorrow's
Watchlist row logged from 25 Sep on still had NULL ret_1d / ret_5d — and the
watchlist's "Past results", follow-through hit rate and the digest's record
label were frozen at 23 Sep.

What a run does:
    1. backfill_returns(max_rows=200) in passes. Each pass skips rows an
       earlier pass already tried, so a row that can't fill yet (the
       provider hasn't published the bar, dead ticker) is tried once per run
       instead of holding a slot in every batch. Stops when a pass has
       nothing left to scan, or when a pass fetched no history at all
       (provider/network outage — exits 1 so the job goes red).
    2. backfill_sector_returns() — stored bhavcopy closes only, no network.

Usage (targets whatever trade_store resolves: DATABASE_URL, else local SQLite):
    py tools/backfill_ledger.py
    py tools/backfill_ledger.py --batch 200 --max-passes 50

Scheduled in .github/workflows/refresh-eod-data.yml after the bhavcopy step.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, Optional, Set
from urllib.parse import urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _target_label(store) -> str:
    url = store._database_url()
    if not url:
        return f"SQLite · {os.path.abspath(store._SQLITE_PATH)}"
    p = urlparse(url)
    return f"Postgres · {p.hostname}/{(p.path or '/').lstrip('/')}"  # never print credentials


def run_backfill(*, batch: int = 200, max_passes: int = 50,
                 log=print) -> Dict[str, object]:
    """Run the return passes then the sector pass. Returns totals plus
    "outage": True when a pass had rows to scan but fetched no history."""
    from analysis import verdict_ledger as vl

    vl.ensure_schema()          # unlike the backfills, raises if the DB is unreachable
    attempted: Set[int] = set()
    totals: Dict[str, object] = {"passes": 0, "scanned": 0, "updated": 0, "outage": False}
    for n in range(1, max_passes + 1):
        stats = vl.backfill_returns(max_rows=batch, attempted=attempted)
        if not stats["scanned"]:
            break
        totals["passes"] = n
        totals["scanned"] += stats["scanned"]
        totals["updated"] += stats["updated"]
        log(f"pass {n}: scanned {stats['scanned']}, updated {stats['updated']}, "
            f"tickers fetched {stats['tickers_fetched']}")
        if not stats["tickers_fetched"]:
            totals["outage"] = True
            log("no price history fetched (NIFTY included) — provider or network down; stopping")
            break
    else:
        log(f"stopped at --max-passes {max_passes}; the rest is picked up next run")

    sector = vl.backfill_sector_returns()
    totals["sector_candidates"] = sector["candidates"]
    totals["sector_updated"] = sector["updated"]
    return totals


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--batch", type=int, default=200, help="rows per backfill pass")
    ap.add_argument("--max-passes", type=int, default=50, help="safety cap on passes")
    args = ap.parse_args(argv)

    import trade_store
    print(f"target: {_target_label(trade_store)}")
    t = run_backfill(batch=args.batch, max_passes=args.max_passes)
    print(f"forward returns: {t['updated']} of {t['scanned']} rows updated in {t['passes']} pass(es)")
    print(f"sector returns:  {t['sector_updated']} of {t['sector_candidates']} rows updated")
    return 1 if t["outage"] else 0


if __name__ == "__main__":
    sys.exit(main())
