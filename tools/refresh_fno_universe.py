"""
tools/refresh_fno_universe.py — regenerate data/nse_fno_underlyings.csv from
NSE's official F&O market-lot file (the list of stock underlyings with live
futures & options contracts).

data/fno_universe.py reads that CSV at import time (no network). This tool is
the only thing that touches the network for it. Columns: Symbol, Company, LotSize
(lot size for the near-month contract). Index underlyings (NIFTY, BANKNIFTY, …)
are excluded — the universe is used to gate per-stock positioning.

Sources, tried in order:
  1. https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv  (static archive)
  2. https://www.nseindia.com/api/underlying-information          (JSON, no lot size)

Usage:
    py tools/refresh_fno_universe.py            # dry run: fetch + print diff, write nothing
    py tools/refresh_fno_universe.py --apply    # also rewrite the CSV

After --apply, run tests/test_fno_universe*.py.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.request
from typing import Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(_ROOT, "data", "nse_fno_underlyings.csv")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MKTLOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
UNDERLYING_API = "https://www.nseindia.com/api/underlying-information"
FIELDS = ["Symbol", "Company", "LotSize"]
_STOCK_SECTION = "DERIVATIVES ON INDIVIDUAL SECURITIES"


def _get(url: str) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA, "Accept": "*/*", "Referer": "https://www.nseindia.com/"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8-sig", errors="replace")
    except Exception as exc:  # noqa: BLE001 — caller tries the next source
        print(f"  {url}: {exc}", file=sys.stderr)
        return None


def parse_mktlots(text: str) -> List[Dict[str, str]]:
    """fo_mktlots.csv -> stock rows. Index rows sit above the
    'Derivatives on Individual Securities' separator and are skipped."""
    rows: List[Dict[str, str]] = []
    in_stocks = False
    for rec in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in rec]
        if len(cells) < 2 or not cells[1]:
            continue
        if cells[0].upper() == _STOCK_SECTION:
            in_stocks = True
            continue
        if not in_stocks or cells[1].upper() == "SYMBOL":
            continue
        lot = cells[2] if len(cells) > 2 else ""
        rows.append({"Symbol": cells[1].upper(), "Company": cells[0], "LotSize": lot})
    return _dedupe(rows)


def parse_underlying_api(text: str) -> List[Dict[str, str]]:
    """NSE underlying-information JSON -> stock rows (no lot size)."""
    data = (json.loads(text) or {}).get("data") or {}
    rows = [{"Symbol": str(u.get("symbol", "")).strip().upper(),
             "Company": str(u.get("underlying", "")).strip(), "LotSize": ""}
            for u in data.get("UnderlyingList") or [] if u.get("symbol")]
    return _dedupe(rows)


def _dedupe(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen: Dict[str, Dict[str, str]] = {}
    for r in rows:
        seen.setdefault(r["Symbol"], r)
    return sorted(seen.values(), key=lambda r: r["Symbol"])


def fetch() -> List[Dict[str, str]]:
    text = _get(MKTLOTS_URL)
    rows = parse_mktlots(text) if text else []
    if not rows:
        text = _get(UNDERLYING_API)
        rows = parse_underlying_api(text) if text else []
    return rows


def load_existing(path: Optional[str] = None) -> List[Dict[str, str]]:
    path = path or OUT_PATH
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def diff(old: List[Dict[str, str]], new: List[Dict[str, str]]) -> Dict[str, list]:
    o = {r["Symbol"] for r in old}
    n = {r["Symbol"] for r in new}
    return {"added": sorted(n - o), "removed": sorted(o - n)}


def write(rows: List[Dict[str, str]], path: Optional[str] = None) -> None:
    path = path or OUT_PATH
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="rewrite the CSV (default: dry run)")
    args = ap.parse_args(argv)

    rows = fetch()
    # Sanity floor: NSE has carried 180-230 stock underlyings since 2023.
    if len(rows) < 100:
        print(f"Refusing: only {len(rows)} underlyings parsed — source changed or blocked.",
              file=sys.stderr)
        return 1
    d = diff(load_existing(), rows)
    print(f"{len(rows)} F&O stock underlyings  (+{len(d['added'])} / -{len(d['removed'])})")
    if d["added"]:
        print("  added:  ", " ".join(d["added"]))
    if d["removed"]:
        print("  removed:", " ".join(d["removed"]))
    if args.apply:
        write(rows)
        print(f"wrote {OUT_PATH}")
    else:
        print("dry run — pass --apply to rewrite the CSV")
    return 0


if __name__ == "__main__":
    sys.exit(main())
