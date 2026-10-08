"""
tools/refresh_universe.py — regenerate data/nse_index_constituents.csv from NSE's
official index constituent CSVs (nsearchives / niftyindices archives).

The universe in data/universe.py is static on purpose (no network at import).
Its index-backed tiers are read from data/nse_index_constituents.csv, which this
tool rewrites. Columns: Symbol, Company, Industry, Index — Index is the narrowest
tier the symbol belongs to (NIFTY50 > NEXT50 > MIDCAP150 > SMALLCAP250 >
MICROCAP250 > TOTALMARKET).

Usage:
    py tools/refresh_universe.py            # dry run: fetch + print diff, write nothing
    py tools/refresh_universe.py --apply    # also rewrite the CSV

NSE needs a browser User-Agent; requests are sequential with a pause (be polite).
After --apply, run tests/test_universe*.py and check the "Other" sector count.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
import urllib.request
from typing import Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(_ROOT, "data", "nse_index_constituents.csv")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_HOSTS = ("https://nsearchives.nseindia.com/content/indices/",
          "https://www.niftyindices.com/IndexConstituent/")

# Tier name -> candidate file names, narrowest tier first (order = precedence).
TIERS: List[tuple] = [
    ("NIFTY50", ["ind_nifty50list.csv"]),
    ("NEXT50", ["ind_niftynext50list.csv"]),
    ("MIDCAP150", ["ind_niftymidcap150list.csv"]),
    ("SMALLCAP250", ["ind_niftysmallcap250list.csv"]),
    ("MICROCAP250", ["ind_niftymicrocap250_list.csv", "ind_niftymicrocap250list.csv"]),
    ("TOTALMARKET", ["ind_niftytotalmarket_list.csv"]),
]
FIELDS = ["Symbol", "Company", "Industry", "Index"]


def _download(name: str) -> Optional[str]:
    for host in _HOSTS:
        try:
            req = urllib.request.Request(host + name, headers={"User-Agent": _UA, "Accept": "text/csv,*/*"})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8-sig", errors="replace")
            if "Symbol" in text.splitlines()[0]:
                return text
        except Exception as exc:  # noqa: BLE001 — try the next host
            print(f"  {host}{name}: {exc}", file=sys.stderr)
        time.sleep(1.5)
    return None


def parse_index_csv(text: str) -> List[Dict[str, str]]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        sym = (r.get("Symbol") or "").strip()
        # DUMMY* rows are NSE placeholders for a pending demerger leg, not tradable.
        series = (r.get("Series") or "EQ").strip()
        if not sym or sym.upper().startswith("DUMMY") or series not in ("EQ", "BE", "BZ", ""):
            continue
        rows.append({"Symbol": sym, "Company": (r.get("Company Name") or "").strip(),
                     "Industry": (r.get("Industry") or "").strip()})
    return rows


def build(fetched: Dict[str, List[Dict[str, str]]]) -> List[Dict[str, str]]:
    """Merge per-tier rows; each symbol keeps its narrowest tier."""
    out: Dict[str, Dict[str, str]] = {}
    for tier, _ in TIERS:
        for r in fetched.get(tier, []):
            if r["Symbol"] not in out:
                out[r["Symbol"]] = {**r, "Index": tier}
    return list(out.values())


def load_existing(path: str = OUT_PATH) -> List[Dict[str, str]]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def diff(old: List[Dict[str, str]], new: List[Dict[str, str]]) -> Dict[str, list]:
    o = {r["Symbol"]: r["Index"] for r in old}
    n = {r["Symbol"]: r["Index"] for r in new}
    return {"added": sorted(set(n) - set(o)), "removed": sorted(set(o) - set(n)),
            "retiered": sorted((s, o[s], n[s]) for s in set(o) & set(n) if o[s] != n[s])}


def write(rows: List[Dict[str, str]], path: str = OUT_PATH) -> None:
    order = {t: i for i, (t, _) in enumerate(TIERS)}
    rows = sorted(rows, key=lambda r: (order[r["Index"]], r["Symbol"]))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="rewrite the CSV (default: dry run)")
    args = ap.parse_args(argv)

    fetched: Dict[str, List[Dict[str, str]]] = {}
    for tier, names in TIERS:
        text = None
        for name in names:
            text = _download(name)
            if text:
                break
        if not text:
            print(f"FAILED to fetch {tier}; aborting (nothing written).", file=sys.stderr)
            return 1
        fetched[tier] = parse_index_csv(text)
        print(f"{tier:<12} {len(fetched[tier]):>4} rows")
        time.sleep(1.5)

    new = build(fetched)
    d = diff(load_existing(), new)
    print(f"total {len(new)} symbols | added {len(d['added'])} | removed {len(d['removed'])} "
          f"| re-tiered {len(d['retiered'])}")
    for k in ("added", "removed", "retiered"):
        if d[k]:
            print(f"{k}: {d[k]}")
    if args.apply:
        write(new)
        print(f"wrote {OUT_PATH}")
    else:
        print("dry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
