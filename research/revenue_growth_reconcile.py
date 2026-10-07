"""
research/revenue_growth_reconcile.py — why did revenue growth go from ρ≈+0.14
(docs/FUNDAMENTAL_QUALITY_REPORT.md, 2026-06-11) to a per-date IC of ≈−0.02
(docs/REVENUE_GROWTH_BY_REGIME_2026-10-06.md, PR #301)?

READ-ONLY research. No production change. Educational only.

Candidate causes, isolated one at a time on ONE rebuilt panel:
  1. statement SOURCE — the original ran on Yahoo-only statements (the Screener.in
     merge-fill landed 2026-08-30, a5b607c); #301 ran on Yahoo + Screener merged.
  2. growth DEFINITION — fundamental_quality._growth_cagr annualises from the
     OLDEST to the newest usable statement. With Yahoo's ~4 annual statements
     that is a 1–3y growth rate; with Screener's 10–12y history it becomes a
     5–10y CAGR — a different factor. Variants: full-history CAGR, last-4
     statements (Yahoo-depth), last-2 (YoY).
  3. universe — tickers Yahoo-only can score vs the full nifty500 panel.
  4. window — original rev_g sample dates (2023-07 → 2025-05, the last date with
     12m forward room in June 2026) vs 2022-10 → 2026-04.
  5. statistic — pooled Spearman across all rows vs mean of per-date ICs
     (Newey-West t, lags = 6 monthly periods for a 6m horizon), plus pooled on
     date-demeaned ranks.

Reuses research.fundamental_quality (LAG_DAYS, _growth_cagr — same
point-in-time rule: statements with period_end ≤ t − 180d) and
research.fundamental_growth_regime.newey_west_t. Statements are fetched per
provider (Yahoo, Screener) and merged here with the service's semantics
(Yahoo primary, Screener fills None / extends history).

Run:
  py -m research.revenue_growth_reconcile               # fetch + analyse
  py -m research.revenue_growth_reconcile --from-cache  # analyse cached inputs
  py -m research.revenue_growth_reconcile --limit 40    # quick check
Outputs (research/output/): rgr_statements.csv, rgr_returns.csv, rgr_grid.csv
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

OUT_DIR = os.path.join(_ROOT, "research", "output")
STMT_CSV = os.path.join(OUT_DIR, "rgr_statements.csv")
RET_CSV = os.path.join(OUT_DIR, "rgr_returns.csv")
GRID_CSV = os.path.join(OUT_DIR, "rgr_grid.csv")

SAMPLE_STEP = 21
H6 = 126
NW_LAGS = H6 // SAMPLE_STEP
MIN_XSEC = 30
ORIG_WINDOW = ("2023-07-01", "2025-05-31")     # original report's rev_g sample dates
FULL_WINDOW = ("2022-10-01", "2026-12-31")
DEFS = {"cagr_full": None, "last4": 4, "yoy": 2}   # statements kept (newest n)


# ─────────────────────────────────────────────────────────────────────────────
# Build (network)
# ─────────────────────────────────────────────────────────────────────────────

def _provider_rows(provider, tickers: List[str], source: str, workers: int) -> List[Dict]:
    from analysis.fundamentals.service import FundamentalsService
    svc = FundamentalsService(providers=[provider])

    def one(t):
        try:
            cf = svc.get_fundamentals(t)
            return [{"ticker": t, "source": source, "pe": str(s.period.period_end),
                     "revenue": s.revenue}
                    for s in (cf.income_statements or [])
                    if s.period and s.period.period_end]
        except Exception:
            return []

    rows: List[Dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, t) for t in tickers]
        for k, f in enumerate(as_completed(futs), 1):
            rows.extend(f.result())
            if k % 50 == 0:
                print(f"  {source} {k}/{len(tickers)}")
    return rows


def build(limit: int = 0, workers: int = 6) -> None:
    from analysis.fundamentals.providers.screener_fundamentals import ScreenerFundamentalProvider
    from analysis.fundamentals.providers.yahoo_fundamentals import YahooFundamentalProvider
    from data.universe import get_universe
    from research.portfolio_fit_efficacy import _fetch_closes

    tickers = sorted(get_universe("nifty500"))
    if limit:
        tickers = tickers[:limit]
    t0 = time.time()
    print(f"REVENUE GROWTH RECONCILE | {len(tickers)} tickers")
    rows = _provider_rows(YahooFundamentalProvider(), tickers, "yahoo", workers)
    print(f"Yahoo statements: {len(rows)} [{time.time()-t0:.0f}s]")
    rows += _provider_rows(ScreenerFundamentalProvider(), tickers, "screener", max(2, workers // 2))
    print(f"All statements: {len(rows)} [{time.time()-t0:.0f}s]")
    os.makedirs(OUT_DIR, exist_ok=True)
    pd.DataFrame(rows).to_csv(STMT_CSV, index=False, encoding="utf-8")

    closes = _fetch_closes(tickers, workers=12)
    didx = closes.index
    start = int(didx.searchsorted(pd.Timestamp(FULL_WINDOW[0])))
    out = []
    for pos in range(start, len(didx) - H6 - 1, SAMPLE_STEP):
        c0, c6 = closes.iloc[pos], closes.iloc[pos + H6]
        f = (c6 / c0 - 1) * 100
        ok = np.isfinite(c0) & (c0 > 0) & np.isfinite(c6)
        for tk in f.index[ok]:
            out.append({"date": str(didx[pos])[:10], "ticker": tk, "fwd_6m": float(f[tk])})
    pd.DataFrame(out).to_csv(RET_CSV, index=False, encoding="utf-8")
    print(f"Returns: {len(out)} rows [{time.time()-t0:.0f}s]")


# ─────────────────────────────────────────────────────────────────────────────
# Analysis (pure)
# ─────────────────────────────────────────────────────────────────────────────

def statement_series(stmts: pd.DataFrame) -> Dict[str, Dict[str, list]]:
    """{source: {ticker: [(pe, revenue, origin), ...] sorted}} for yahoo / merged."""
    s = stmts.copy()
    s["pe"] = pd.to_datetime(s["pe"])
    out: Dict[str, Dict[str, list]] = {"yahoo": {}, "merged": {}}
    for tk, g in s.groupby("ticker"):
        y = g[g["source"] == "yahoo"].set_index("pe")["revenue"]
        sc = g[g["source"] == "screener"].set_index("pe")["revenue"]
        y = y[~y.index.duplicated()]
        sc = sc[~sc.index.duplicated()]
        out["yahoo"][tk] = [(d, v, "y") for d, v in y.sort_index().items()]
        merged = []
        for d in sorted(set(y.index) | set(sc.index)):
            yv = y.get(d)
            if yv is not None and pd.notna(yv):
                merged.append((d, float(yv), "y"))
            elif d in sc.index and pd.notna(sc.get(d)):
                merged.append((d, float(sc.get(d)), "s"))
            else:
                merged.append((d, None, "-"))
        out["merged"][tk] = merged
    return out


def rev_growth(series: list, t: pd.Timestamp, keep: Optional[int]) -> tuple:
    """(growth %, mixed_source) at t; same point-in-time rule as the original."""
    from research.fundamental_quality import LAG_DAYS, _growth_cagr
    cutoff = t - pd.Timedelta(days=LAG_DAYS)
    usable = [p for p in series
              if p[0] <= cutoff and p[1] is not None and np.isfinite(p[1])]
    if keep:
        usable = usable[-keep:]
    g = _growth_cagr([(d, v) for d, v, _ in usable])
    valid = [o for _, v, o in usable if v is not None and np.isfinite(v)]
    mixed = bool(valid) and valid[0] != valid[-1]
    return g, mixed


def build_panel(stmts: pd.DataFrame, rets: pd.DataFrame) -> pd.DataFrame:
    ser = statement_series(stmts)
    rows = []
    for d, g in rets.groupby("date"):
        t = pd.Timestamp(d)
        for tk, f6 in zip(g["ticker"], g["fwd_6m"]):
            row = {"date": d, "ticker": tk, "fwd_6m": f6}
            for src in ("yahoo", "merged"):
                for name, keep in DEFS.items():
                    gv, mixed = rev_growth(ser[src].get(tk, []), t, keep)
                    row[f"{src}_{name}"] = gv
                    if src == "merged" and name == "cagr_full":
                        row["merged_mixed"] = mixed
            rows.append(row)
    return pd.DataFrame(rows)


def newey_west_t(x, lags: int = NW_LAGS) -> float:
    from research.fundamental_growth_regime import newey_west_t as _nw
    return _nw(np.asarray(x, float), lags)


def stats(df: pd.DataFrame, col: str) -> Dict:
    d = df.dropna(subset=[col, "fwd_6m"])
    pooled = float(d[col].rank().corr(d["fwd_6m"].rank())) if len(d) >= 30 else np.nan
    ics = []
    for _, g in d.groupby("date"):
        if len(g) >= MIN_XSEC:
            ics.append(g[col].rank().corr(g["fwd_6m"].rank()))
    ics = np.array(ics, float)
    if len(d):
        r1 = d.groupby("date")[col].rank(pct=True)
        r2 = d.groupby("date")["fwd_6m"].rank(pct=True)
        demeaned = float(r1.corr(r2))
    else:
        demeaned = np.nan
    return {"rows": len(d), "dates": int(len(ics)), "pooled": round(pooled, 4),
            "pooled_xsec_ranks": round(demeaned, 4),
            "mean_ic": round(float(ics.mean()), 4) if len(ics) else np.nan,
            "nw_t": round(newey_west_t(ics), 2) if len(ics) >= 3 else np.nan,
            "hit_pct": round(float((ics > 0).mean() * 100), 1) if len(ics) else np.nan}


def grid(panel: pd.DataFrame) -> pd.DataFrame:
    yahoo_univ = set(panel.loc[panel["yahoo_cagr_full"].notna(), "ticker"])
    out = []
    for src in ("yahoo", "merged"):
        for name in DEFS:
            col = f"{src}_{name}"
            for wname, (a, b) in [("orig", ORIG_WINDOW), ("full", FULL_WINDOW)]:
                w = panel[(panel["date"] >= a) & (panel["date"] <= b)]
                for uname, u in [("yahoo_covered", w[w["ticker"].isin(yahoo_univ)]),
                                 ("all", w)]:
                    out.append({"source": src, "definition": name, "window": wname,
                                "universe": uname, **stats(u, col)})
    # Cross-source mixing inside the merged full CAGR (oldest point Screener, newest Yahoo)
    for mixed in (False, True):
        u = panel[panel["merged_mixed"] == mixed]
        out.append({"source": "merged", "definition": f"cagr_full|mixed={mixed}",
                    "window": "full", "universe": "all", **stats(u, "merged_cagr_full")})
    return pd.DataFrame(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Reconcile revenue-growth IC across studies")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--from-cache", action="store_true")
    args = ap.parse_args(argv)
    if not args.from_cache:
        build(args.limit, args.workers)
    if not (os.path.exists(STMT_CSV) and os.path.exists(RET_CSV)):
        print("Cached inputs missing — run without --from-cache first.")
        return 1
    stmts, rets = pd.read_csv(STMT_CSV), pd.read_csv(RET_CSV)
    if stmts.empty or rets.empty:
        print("No observations.")
        return 1
    rets["date"] = rets["date"].astype(str)
    panel = build_panel(stmts, rets)
    g = grid(panel)
    g.to_csv(GRID_CSV, index=False, encoding="utf-8")
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    n_hist = stmts.groupby(["source", "ticker"]).size().groupby("source").median()
    print("Median annual statements per ticker:", n_hist.to_dict())
    print(g.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
