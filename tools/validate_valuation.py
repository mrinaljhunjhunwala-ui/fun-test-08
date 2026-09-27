#!/usr/bin/env python
"""tools/validate_valuation.py — E1-v2 valuation regression diagnostic.

Runs the E1-v2 Valuation Decision Layer LIVE over the tickers in a golden snapshot and
reports drift (posture / confidence / branch / guards). This is a **diagnostic script, NOT
a pytest test** — it makes live Yahoo network calls and must NOT run in CI. The offline
guarantee lives in tests/test_valuation_golden_snapshot.py.

Usage:
    python tools/validate_valuation.py [--snapshot PATH] [--update]

    (no flags)   load the snapshot, run live, print a regression report (drift / unchanged / fail)
    --update     run live over the seed tickers and OVERWRITE the snapshot with current results
    --snapshot   path to the golden snapshot (default: data/valuation_golden_snapshot.json)

Output (stdout):
    PASS  62/62 unchanged
    DRIFT HDFCBANK: was DEMANDING_VS_ROE/high, now REASONABLE/medium
    FAIL  <TICKER>: fetch error
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DEFAULT_SNAPSHOT = os.path.join("data", "valuation_golden_snapshot.json")

# The 62 V1-validated tickers (docs/V1_NSE_VALIDATION_REPORT.md) — the regression seed.
SEED_TICKERS = [
    # Nifty50
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "ITC", "LT",
    "BHARTIARTL", "MARUTI",
    # Banks
    "SBIN", "KOTAKBANK", "AXISBANK", "BANKBARODA", "PNB", "INDUSINDBK", "FEDERALBNK",
    # NBFC
    "BAJFINANCE", "BAJAJFINSV", "CHOLAFIN", "MUTHOOTFIN", "SHRIRAMFIN", "SBICARD", "LICHSGFIN",
    # Insurance
    "ICICIGI", "ICICIPRULI", "HDFCLIFE", "SBILIFE",
    # PSU
    "ONGC", "NTPC", "POWERGRID", "BHEL", "SAIL", "GAIL", "IOC", "COALINDIA", "NMDC",
    # CapitalGoods
    "SIEMENS", "ABB", "CUMMINSIND", "THERMAX", "BEL", "HAVELLS",
    # Consumer
    "NESTLEIND", "BRITANNIA", "DABUR", "MARICO", "TATACONSUM", "COLPAL",
    # IT
    "WIPRO", "HCLTECH", "TECHM", "LTM", "PERSISTENT", "COFORGE",
    # Midcap
    "SRF", "DEEPAKNITR", "POLYCAB", "DIXON", "TATAELXSI", "TATASTEEL", "HINDALCO",
]


def assess_ticker(ticker: str) -> dict:
    """Run E1-v2 live for one ticker. Returns the comparable result fields + the captured
    ValuationInputs (for deterministic offline replay) — or raises on fetch failure."""
    import dataclasses
    from analysis.fundamentals.service import default_service
    from analysis.fundamentals import analytics as A
    from analysis.fundamentals.valuation import build_valuation_context
    from analysis.fundamentals.valuation_decision import build_valuation_inputs, assess
    from analysis.sector_classification import classify_sector
    from data.universe import get_sector

    tk = ticker if ticker.endswith(".NS") else ticker + ".NS"
    cf = default_service().get_fundamentals(tk)
    an = A.compute_all(cf)
    sp = classify_sector(get_sector(tk), name=cf.company_name)
    vc = build_valuation_context(cf, sp)
    inp = build_valuation_inputs(vc, an, sp, cf)
    va = assess(inp)
    return {
        "posture": va.posture,
        "confidence": va.confidence,
        "branch": va.sector_branch,
        "guards_triggered": [va.triggered_guard] if va.triggered_guard else [],
        # captured inputs → the offline test replays these through assess() (no network)
        "inputs": dataclasses.asdict(inp),
    }


def load_snapshot(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_snapshot(path: str, tickers: dict) -> None:
    payload = {
        "schema_version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source": "docs/V1_NSE_VALIDATION_REPORT.md",
        "tickers": tickers,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="E1-v2 valuation regression diagnostic (live Yahoo; not a CI test).")
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT, help="golden snapshot path")
    ap.add_argument("--update", action="store_true",
                    help="run live over the seed tickers and overwrite the snapshot")
    args = ap.parse_args()

    if args.update:
        tickers = {}
        for t in SEED_TICKERS:
            try:
                tickers[t] = assess_ticker(t)
            except Exception as e:
                print(f"FAIL  {t}: fetch error ({type(e).__name__})")
        write_snapshot(args.snapshot, tickers)
        print(f"\nUPDATED {args.snapshot} with {len(tickers)}/{len(SEED_TICKERS)} tickers.")
        return 0

    try:
        snap = load_snapshot(args.snapshot)
    except Exception as e:
        print(f"ERROR: cannot load snapshot {args.snapshot}: {e}")
        return 2
    golden = snap.get("tickers", {})
    unchanged = drift = fail = 0
    for t, exp in sorted(golden.items()):
        try:
            cur = assess_ticker(t)
        except Exception:
            print(f"FAIL  {t}: fetch error (known Yahoo gap)")
            fail += 1
            continue
        if (cur["posture"] == exp.get("posture")
                and cur["confidence"] == exp.get("confidence")
                and cur["branch"] == exp.get("branch")
                and cur["guards_triggered"] == exp.get("guards_triggered", [])):
            unchanged += 1
        else:
            drift += 1
            print(f"DRIFT {t}: was {exp.get('posture')}/{exp.get('confidence')}, "
                  f"now {cur['posture']}/{cur['confidence']}")
    total = len(golden)
    print(f"\nPASS  {unchanged}/{total} unchanged" if drift == 0 and fail == 0
          else f"\nRESULT {unchanged}/{total} unchanged · {drift} drifted · {fail} fetch-failed")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
